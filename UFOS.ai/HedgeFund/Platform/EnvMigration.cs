using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using UFOS.ai.Shared;

namespace UFOS.ai.HedgeFund.Platform
{
    // =====================================================================================================================
    // S10-E (user decision 2026-10-24 16:15): ".env removed — Credential Manager only, ALL modules". This file is the ONE
    // place in the solution that still reads .env files: the one-time "Import from old .env files…" migration of
    // ⚙ Keys & data, run by the user on his PC. Contract: scratchpad ENV_API.md §5 (→ SPEC).
    //
    //  1. Scan: every .env of the solution (UFOS.ai folder, recursive, skipping bin/obj/.venv/node_modules/.git/__pycache__,
    //     plus a .env directly in the repository root). Values are kept in memory ONLY for known key / setting names;
    //     everything else is recorded by NAME only.
    //  2. Plan (shown in ONE dialog, names only): keys → Credential Manager (new / already the same / stored differs → keep or
    //     replace / different values in several files → pick a file); known settings → the module's settings file, only when
    //     they differ from the code default; process switches (ALLOW_REMOTE_ACCESS, WS_HOST, …) dropped; unknown names dropped
    //     unless the user ticks "keep in file".
    //  3. Apply: write each key, verify by read-back (equal value + equal length, never displayed); write settings and re-read
    //     them; then rewrite each .env WITHOUT every line that was handled; a file with nothing left is DELETED. No backup copy
    //     of a .env is ever made (secrets must not remain in plaintext). A line whose import failed stays in its file.
    //  4. Report: per key / setting / file — names and actions only; audit: POST /v2/settings/keys/changed "imported".
    // =====================================================================================================================

    internal enum EnvModule { MainApp, HedgeFund, GlobalMonitoring, LiveStreamAgent, Other }

    internal enum EnvSettingKind { Text, Integer, Number }

    /// <summary>A known non-secret .env setting and where it moves (ENV_API §4; defaults from ENV_API_DEFAULTS.json).</summary>
    internal sealed record EnvSettingSpec(EnvModule Module, string EnvName, string JsonKey, EnvSettingKind Kind, string DefaultText);

    internal enum EnvEntryKind { Key, Setting, ProcessSwitch, Unknown }

    /// <summary>One KEY=VALUE name of a .env file (all its lines; the last parseable one counts, like python-dotenv).</summary>
    internal sealed class EnvEntry
    {
        public string Name { get; init; } = "";
        public EnvEntryKind Kind { get; init; }
        public SecretInfo? Secret { get; init; }
        public EnvSettingSpec? Setting { get; set; }
        public List<int> LineIndexes { get; } = new();
        /// <summary>Line number (1-based) of a line that could not be parsed (e.g. an unclosed quote); null = fine.</summary>
        public int? BrokenLine { get; set; }
        internal string? Value { get; set; }   // ONLY for Key / Setting; never shown, logged or returned to the UI
        /// <summary>The user's choice for an unknown name: keep its lines in the file (default: drop).</summary>
        public bool KeepInFile { get; set; }
        /// <summary>Set by Apply: these lines may be removed from the file.</summary>
        public bool Handled { get; set; }
    }

    internal sealed class EnvFileScan
    {
        public string FilePath { get; init; } = "";
        public string DisplayPath { get; init; } = "";
        public EnvModule Module { get; init; }
        public string? ReadError { get; init; }
        internal List<string> RawLines { get; init; } = new();
        public List<EnvEntry> Entries { get; } = new();
    }

    internal enum KeyPlanState { New, SameAsStored, DiffersFromStored, SeveralValues, StoredUnreadable, StoreError, Broken }

    /// <summary>What happens to one credential. Choice: -1 = keep the stored key, n = use the value of source group n.</summary>
    internal sealed class KeyPlan
    {
        public SecretInfo Secret { get; init; } = null!;
        public List<(EnvFileScan File, EnvEntry Entry)> Sources { get; } = new();
        /// <summary>Distinct values, each with the files that contain it (values themselves are private).</summary>
        public List<List<(EnvFileScan File, EnvEntry Entry)>> ValueGroups { get; } = new();
        public KeyPlanState State { get; set; }
        public bool StoredPresent { get; set; }
        public string? Problem { get; set; }
        public int? Choice { get; set; }
        public string? Outcome { get; set; }
        public bool Failed { get; set; }
        public bool Imported { get; set; }

        public bool NeedsChoice => State is KeyPlanState.DiffersFromStored or KeyPlanState.SeveralValues or KeyPlanState.StoredUnreadable;
    }

    internal sealed class SettingPlan
    {
        public EnvSettingSpec Spec { get; init; } = null!;
        public List<(EnvFileScan File, EnvEntry Entry)> Sources { get; } = new();
        public JsonNode? Value { get; set; }
        public bool IsDefault { get; set; }
        public string? Problem { get; set; }
        public string? Outcome { get; set; }
        public bool Failed { get; set; }
    }

    internal sealed class EnvMigrationPlan
    {
        public string? SolutionRoot { get; init; }
        public string? Problem { get; init; }
        public List<EnvFileScan> Files { get; } = new();
        public List<KeyPlan> Keys { get; } = new();
        public List<SettingPlan> Settings { get; } = new();
        public List<string> FileOutcomes { get; } = new();
        public bool AnyFileProblem { get; set; }

        /// <summary>Drops every value held in memory (called when the dialog closes, applied or not).</summary>
        public void Forget()
        {
            foreach (var e in Files.SelectMany(f => f.Entries)) e.Value = null;
            foreach (var s in Settings) s.Value = null;
            foreach (var f in Files) f.RawLines.Clear();
        }
    }

    internal static class EnvMigration
    {
        private static readonly Regex KeyPattern = new("^[A-Za-z_][A-Za-z0-9_]*$", RegexOptions.CultureInvariant);
        private static readonly string[] SkipDirs = { "bin", "obj", ".venv", "venv", "node_modules", ".git", "__pycache__", ".vs" };
        private const int MaxDepth = 5;
        private const int MaxEnvBytes = 256 * 1024;

        /// <summary>
        /// Former process / deployment switches (ENV_API §1): dropped from a .env and reported by name. ALLOW_REMOTE_ACCESS, WS_HOST
        /// and HTTP_HOST no longer exist at all (Docker removed 2026-10-24 16:56 — every backend binds 127.0.0.1); the ports are fixed.
        /// CORS_ALLOWED_ORIGINS still exists as a process variable, never as a .env setting.
        /// </summary>
        internal static readonly HashSet<string> ProcessSwitches = new(StringComparer.Ordinal)
        {
            "ALLOW_REMOTE_ACCESS", "WS_HOST", "HTTP_HOST", "CORS_ALLOWED_ORIGINS", "WS_PORT", "HEALTH_PORT",
        };

        /// <summary>
        /// Known settings per module (ENV_API §4). Main app: Shared/MainAppSettings.cs. The backend entries mirror
        /// ENV_API_DEFAULTS.json (Agent H) — change both together.
        /// </summary>
        internal static readonly IReadOnlyList<EnvSettingSpec> SettingSpecs = BuildSettingSpecs();

        // ------------------------------------------------------------------ scan + plan

        public static EnvMigrationPlan BuildPlan()
        {
            var root = FindSolutionRoot();
            if (root == null)
                return new EnvMigrationPlan { Problem = "The UFOS.ai folder (with UFOS.ai.csproj) was not found above the app — nothing to import." };

            var plan = new EnvMigrationPlan { SolutionRoot = root };
            foreach (var path in DiscoverEnvFiles(root)) plan.Files.Add(ScanFile(root, path));

            // keys: group the sources of each credential by distinct value
            foreach (var secret in CredentialStore.Secrets)
            {
                var sources = plan.Files.SelectMany(f => f.Entries.Where(e => e.Kind == EnvEntryKind.Key && e.Secret?.Name == secret.Name)
                                                                  .Select(e => (File: f, Entry: e))).ToList();
                if (sources.Count == 0) continue;
                var kp = new KeyPlan { Secret = secret };
                kp.Sources.AddRange(sources);
                PlanKey(kp);
                plan.Keys.Add(kp);
            }

            // settings: per (module, json key)
            foreach (var group in plan.Files.SelectMany(f => f.Entries.Where(e => e.Kind == EnvEntryKind.Setting && e.Setting != null)
                                                                      .Select(e => (File: f, Entry: e)))
                                            .GroupBy(x => (x.Entry.Setting!.Module, x.Entry.Setting.JsonKey)))
            {
                var sp = new SettingPlan { Spec = group.First().Entry.Setting! };
                sp.Sources.AddRange(group);
                PlanSetting(sp);
                plan.Settings.Add(sp);
            }
            return plan;
        }

        private static void PlanKey(KeyPlan kp)
        {
            var broken = kp.Sources.FirstOrDefault(s => s.Entry.BrokenLine != null);
            if (broken.Entry != null)
            {
                kp.State = KeyPlanState.Broken;
                kp.Problem = $"{broken.Entry.Name} on line {broken.Entry.BrokenLine} of {broken.File.DisplayPath} could not be read — fix or remove that line by hand";
                return;
            }
            foreach (var s in kp.Sources.Where(s => !string.IsNullOrWhiteSpace(s.Entry.Value)))
            {
                var group = kp.ValueGroups.FirstOrDefault(g => string.Equals(g[0].Entry.Value!.Trim(), s.Entry.Value!.Trim(), StringComparison.Ordinal));
                if (group == null) kp.ValueGroups.Add(new List<(EnvFileScan, EnvEntry)> { s }); else group.Add(s);
            }
            if (kp.ValueGroups.Count == 0)
            {
                // only empty assignments: nothing to import, the lines can go
                kp.State = KeyPlanState.SameAsStored;
                kp.Problem = "empty in the .env file(s) — nothing to import";
                return;
            }
            KeyState stored;
            try { stored = CredentialStore.GetState(kp.Secret.Name); }
            catch (CredentialStoreException ex)
            {
                kp.State = KeyPlanState.StoreError;
                kp.Problem = ex.Message;
                return;
            }
            kp.StoredPresent = stored != KeyState.NotSet;
            if (kp.ValueGroups.Count > 1)
            {
                kp.State = KeyPlanState.SeveralValues;   // the user picks a file (or keeps the stored key)
                return;
            }
            if (stored == KeyState.NotSet) { kp.State = KeyPlanState.New; kp.Choice = 0; return; }
            if (stored == KeyState.Unreadable) { kp.State = KeyPlanState.StoredUnreadable; return; }
            bool same;
            try { same = CredentialStore.ValueEquals(kp.Secret.Name, kp.ValueGroups[0][0].Entry.Value!); }
            catch (CredentialStoreException ex)
            {
                kp.State = KeyPlanState.StoreError;
                kp.Problem = ex.Message;
                return;
            }
            if (same) { kp.State = KeyPlanState.SameAsStored; kp.Choice = -1; return; }
            kp.State = KeyPlanState.DiffersFromStored;
            kp.Choice = -1; // default: keep the stored key (the user can choose the file's value)
        }

        private static void PlanSetting(SettingPlan sp)
        {
            var broken = sp.Sources.FirstOrDefault(s => s.Entry.BrokenLine != null);
            if (broken.Entry != null)
            {
                sp.Problem = $"line {broken.Entry.BrokenLine} of {broken.File.DisplayPath} could not be read — left in the file";
                return;
            }
            var values = sp.Sources.Select(s => (s.Entry.Value ?? "").Trim()).Distinct(StringComparer.Ordinal).ToList();
            if (values.Count > 1)
            {
                sp.Problem = "different values in " + string.Join(" and ", sp.Sources.Select(s => s.File.DisplayPath)) + " — not moved, left in the files";
                return;
            }
            if (values[0].Length == 0)
            {
                sp.IsDefault = true;   // empty assignment = "use the default", as the backends treated it
                return;
            }
            if (!TryConvert(sp.Spec.Kind, values[0], out var node, out var problem))
            {
                sp.Problem = problem + " — not moved, left in the file";
                return;
            }
            TryConvert(sp.Spec.Kind, sp.Spec.DefaultText, out var def, out _);
            sp.IsDefault = def != null && JsonNode.DeepEquals(node, def);
            sp.Value = node;
        }

        private static bool TryConvert(EnvSettingKind kind, string text, out JsonNode? node, out string problem)
        {
            node = null;
            problem = "";
            switch (kind)
            {
                case EnvSettingKind.Integer:
                    if (long.TryParse(text, System.Globalization.NumberStyles.Integer, System.Globalization.CultureInfo.InvariantCulture, out var l))
                    { node = JsonValue.Create(l); return true; }
                    problem = "not a whole number";
                    return false;
                case EnvSettingKind.Number:
                    if (double.TryParse(text, System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var d)
                        && !double.IsNaN(d) && !double.IsInfinity(d))
                    { node = JsonValue.Create(d); return true; }
                    problem = "not a number";
                    return false;
                default:
                    if (text.Length > 500) { problem = "longer than 500 characters"; return false; }
                    node = JsonValue.Create(text);
                    return true;
            }
        }

        // ------------------------------------------------------------------ apply

        /// <summary>Applies the plan (after the user confirmed it). Never throws for a single item; results are in the plan.</summary>
        public static void Apply(EnvMigrationPlan plan)
        {
            foreach (var kp in plan.Keys) ApplyKey(kp);
            ApplySettings(plan);
            foreach (var file in plan.Files.Where(f => f.ReadError == null)) RewriteFile(plan, file);
        }

        private static void ApplyKey(KeyPlan kp)
        {
            void Handled() { foreach (var s in kp.Sources) s.Entry.Handled = true; }
            switch (kp.State)
            {
                case KeyPlanState.Broken:
                case KeyPlanState.StoreError:
                    kp.Failed = true;
                    kp.Outcome = "not imported — " + kp.Problem + " (left in the .env file)";
                    return;
                case KeyPlanState.SameAsStored:
                    Handled();
                    kp.Outcome = kp.Problem != null ? "nothing to import — " + kp.Problem : "already stored with the same value";
                    return;
            }
            if (kp.Choice == null)
            {
                kp.Failed = true;
                kp.Outcome = "not imported — no choice made (left in the .env files)";
                return;
            }
            if (kp.Choice == -1)
            {
                Handled();
                kp.Outcome = "kept the stored key (the .env value" + (kp.ValueGroups.Count > 1 ? "s were" : " was") + " discarded)";
                return;
            }
            var group = kp.ValueGroups[kp.Choice.Value];
            var value = group[0].Entry.Value!.Trim();
            try
            {
                CredentialStore.Set(kp.Secret.Name, value);
                // read-back: same value AND same length (never displayed)
                var ok = CredentialStore.ValueEquals(kp.Secret.Name, value)
                         && CredentialVault.StoredLength(kp.Secret.Target) == Encoding.UTF8.GetByteCount(value);
                if (!ok)
                {
                    kp.Failed = true;
                    kp.Outcome = "stored, but the read-back check failed — the .env line was kept; set the key again in ⚙ Keys & data";
                    return;
                }
                Handled();
                kp.Imported = true;
                kp.Outcome = "imported and verified" + (kp.ValueGroups.Count > 1 ? " (value from " + group[0].File.DisplayPath + ")" : "")
                             + (kp.StoredPresent ? " — replaced the stored key" : "");
            }
            catch (Exception ex) when (ex is CredentialStoreException || ex is CredentialVaultException || ex is ArgumentException)
            {
                kp.Failed = true;
                kp.Outcome = "not imported — " + (ex is ArgumentException ? "the value is empty" : ex.Message) + " (left in the .env file)";
            }
        }

        private static void ApplySettings(EnvMigrationPlan plan)
        {
            foreach (var sp in plan.Settings)
            {
                if (sp.Problem != null) { sp.Failed = true; sp.Outcome = sp.Problem; continue; }
                if (sp.IsDefault)
                {
                    foreach (var s in sp.Sources) s.Entry.Handled = true;
                    sp.Outcome = "same as the default — not copied";
                }
            }
            foreach (var module in plan.Settings.Where(s => s.Problem == null && !s.IsDefault).GroupBy(s => s.Spec.Module))
            {
                var path = SettingsPath(plan.SolutionRoot!, module.Key);
                if (path == null)
                {
                    foreach (var sp in module) { sp.Failed = true; sp.Outcome = "the module's settings file location is unknown — left in the .env file"; }
                    continue;
                }
                var values = module.ToDictionary(sp => sp.Spec.JsonKey, sp => sp.Value);
                try
                {
                    BackendSettingsFile.SetValues(path, values);
                    var back = BackendSettingsFile.Read(path);
                    foreach (var sp in module)
                    {
                        if (JsonNode.DeepEquals(back[sp.Spec.JsonKey], sp.Value))
                        {
                            foreach (var s in sp.Sources) s.Entry.Handled = true;
                            sp.Outcome = "moved to " + Display(plan.SolutionRoot!, path) + " (" + sp.Spec.JsonKey + ")";
                        }
                        else
                        {
                            sp.Failed = true;
                            sp.Outcome = "written, but the read-back check failed — left in the .env file";
                        }
                    }
                }
                catch (BackendSettingsException ex)
                {
                    foreach (var sp in module) { sp.Failed = true; sp.Outcome = ex.Message + " — left in the .env file"; }
                }
            }
        }

        private static void RewriteFile(EnvMigrationPlan plan, EnvFileScan file)
        {
            foreach (var e in file.Entries)
            {
                if (e.Kind == EnvEntryKind.ProcessSwitch) e.Handled = true;
                else if (e.Kind == EnvEntryKind.Unknown) e.Handled = !e.KeepInFile && e.BrokenLine == null;
            }
            var keep = new SortedSet<int>(file.Entries.Where(e => !e.Handled).SelectMany(e => e.LineIndexes));
            try
            {
                if (keep.Count == 0)
                {
                    File.Delete(file.FilePath);
                    plan.FileOutcomes.Add(file.DisplayPath + ": deleted (everything in it was imported, moved or dropped)");
                    return;
                }
                var sb = new StringBuilder();
                sb.Append("# UFOS.ai no longer reads this file (S10-E, 2026-10-24): keys live in the Windows Credential Manager (⚙ Keys & data),\n");
                sb.Append("# settings in the modules' settings files. The lines below were left because they could not be imported or you chose to keep them.\n");
                foreach (var i in keep) sb.Append(file.RawLines[i]).Append('\n');
                var temp = file.FilePath + "." + Guid.NewGuid().ToString("N") + ".tmp";
                File.WriteAllText(temp, sb.ToString(), new UTF8Encoding(false));
                File.Move(temp, file.FilePath, overwrite: true);
                sb.Clear();
                plan.FileOutcomes.Add(file.DisplayPath + ": rewritten — " + keep.Count + " line" + (keep.Count == 1 ? "" : "s") + " left ("
                                      + string.Join(", ", file.Entries.Where(e => !e.Handled).Select(e => e.Name)) + ")");
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
            {
                plan.AnyFileProblem = true;
                plan.FileOutcomes.Add(file.DisplayPath + ": could not be changed (" + ex.GetType().Name + ") — the keys are stored, "
                                      + "but the file still contains them; delete it by hand");
            }
        }

        // ------------------------------------------------------------------ discovery + parsing

        /// <summary>The UFOS.ai folder (contains UFOS.ai.csproj) above the app's base directory, or null.</summary>
        internal static string? FindSolutionRoot()
        {
            var dir = new DirectoryInfo(AppDomain.CurrentDomain.BaseDirectory);
            for (var i = 0; i < 10 && dir != null; i++, dir = dir.Parent)
                if (File.Exists(Path.Combine(dir.FullName, "UFOS.ai.csproj"))) return dir.FullName;
            return null;
        }

        internal static List<string> DiscoverEnvFiles(string root)
        {
            var found = new List<string>();
            void Walk(string dir, int depth)
            {
                string[] files, dirs;
                try { files = Directory.GetFiles(dir, ".env"); dirs = Directory.GetDirectories(dir); }
                catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException) { return; }
                found.AddRange(files.Where(f => string.Equals(Path.GetFileName(f), ".env", StringComparison.OrdinalIgnoreCase)));
                if (depth >= MaxDepth) return;
                foreach (var d in dirs)
                {
                    var name = Path.GetFileName(d);
                    if (SkipDirs.Contains(name, StringComparer.OrdinalIgnoreCase)) continue;
                    try { if ((File.GetAttributes(d) & FileAttributes.ReparsePoint) != 0) continue; } catch { continue; }
                    Walk(d, depth + 1);
                }
            }
            Walk(root, 0);
            // a .env directly in the repository root (docker-compose folder), not recursive
            var parent = Directory.GetParent(root)?.FullName;
            if (parent != null && File.Exists(Path.Combine(parent, ".env"))) found.Add(Path.Combine(parent, ".env"));
            return found.Distinct(StringComparer.OrdinalIgnoreCase).OrderBy(p => p, StringComparer.OrdinalIgnoreCase).ToList();
        }

        internal static EnvModule ModuleOf(string root, string envPath)
        {
            var rel = Path.GetRelativePath(root, envPath).Replace('\\', '/');
            return rel.ToLowerInvariant() switch
            {
                ".env" => EnvModule.MainApp,
                "hedgefund/backend/.env" or "hedgefund/.env" => EnvModule.HedgeFund,
                "global monitoring/backend/.env" => EnvModule.GlobalMonitoring,
                "livestreamagent/backend/.env" => EnvModule.LiveStreamAgent,
                _ => EnvModule.Other,
            };
        }

        public static string ModuleName(EnvModule m) => m switch
        {
            EnvModule.MainApp => "main window",
            EnvModule.HedgeFund => "Hedge Fund",
            EnvModule.GlobalMonitoring => "Global Monitoring",
            EnvModule.LiveStreamAgent => "LiveStream Agent",
            _ => "unknown module",
        };

        private static string? SettingsPath(string root, EnvModule module) => module switch
        {
            EnvModule.MainApp => MainAppSettings.FilePath,
            EnvModule.HedgeFund => BackendSettingsFile.PathFor(Path.Combine(root, "HedgeFund", "backend")),
            EnvModule.GlobalMonitoring => BackendSettingsFile.PathFor(Path.Combine(root, "Global Monitoring", "backend")),
            EnvModule.LiveStreamAgent => BackendSettingsFile.PathFor(Path.Combine(root, "LiveStreamAgent", "backend")),
            _ => null,
        };

        internal static string Display(string root, string path)
        {
            var rel = Path.GetRelativePath(root, path);
            return rel.StartsWith("..", StringComparison.Ordinal) ? path : rel;
        }

        private static EnvFileScan ScanFile(string root, string path)
        {
            var module = ModuleOf(root, path);
            var display = Display(root, path);
            List<string> lines;
            try
            {
                if (new FileInfo(path).Length > MaxEnvBytes)
                    return new EnvFileScan { FilePath = path, DisplayPath = display, Module = module, ReadError = "larger than 256 KB — not read" };
                lines = File.ReadAllLines(path, Encoding.UTF8).ToList();
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
            {
                return new EnvFileScan { FilePath = path, DisplayPath = display, Module = module, ReadError = "could not be read (" + ex.GetType().Name + ")" };
            }

            var scan = new EnvFileScan { FilePath = path, DisplayPath = display, Module = module, RawLines = lines };
            var byName = new Dictionary<string, EnvEntry>(StringComparer.Ordinal);
            for (var i = 0; i < lines.Count; i++)
            {
                var line = lines[i].TrimStart('﻿').Trim();
                if (line.Length == 0 || line.StartsWith('#')) continue;
                if (line.StartsWith("export ", StringComparison.Ordinal) || line.StartsWith("export\t", StringComparison.Ordinal))
                    line = line.Substring(7).TrimStart();
                var eq = line.IndexOf('=');
                if (eq <= 0) continue;   // not an assignment: dropped with the comments
                var name = line.Substring(0, eq).Trim();
                if (!KeyPattern.IsMatch(name)) continue;

                if (!byName.TryGetValue(name, out var entry))
                {
                    var secret = CredentialStore.Secrets.FirstOrDefault(s => s.EnvNames.Contains(name, StringComparer.Ordinal));
                    var setting = SettingSpecs.FirstOrDefault(s => s.Module == module && s.EnvName == name);
                    var kind = secret != null ? EnvEntryKind.Key
                             : setting != null ? EnvEntryKind.Setting
                             : ProcessSwitches.Contains(name) ? EnvEntryKind.ProcessSwitch
                             : EnvEntryKind.Unknown;
                    entry = new EnvEntry { Name = name, Kind = kind, Secret = secret, Setting = setting };
                    byName[name] = entry;
                    scan.Entries.Add(entry);
                }
                entry.LineIndexes.Add(i);
                if (entry.Kind != EnvEntryKind.Key && entry.Kind != EnvEntryKind.Setting) continue;   // names only
                if (TryParseValue(line.Substring(eq + 1), out var value))
                {
                    entry.Value = value;
                    entry.BrokenLine = null;
                }
                else
                {
                    entry.Value = null;
                    entry.BrokenLine = i + 1;
                }
            }
            return scan;
        }

        /// <summary>dotenv-style value: quotes (with \" and \\ in double quotes), inline " # comment" after unquoted values.</summary>
        internal static bool TryParseValue(string raw, out string value)
        {
            value = "";
            var s = raw.TrimStart();
            if (s.Length == 0) return true;

            var quote = s[0];
            if (quote == '"' || quote == '\'')
            {
                var sb = new StringBuilder();
                for (var i = 1; i < s.Length; i++)
                {
                    var c = s[i];
                    if (quote == '"' && c == '\\' && i + 1 < s.Length && (s[i + 1] == '"' || s[i + 1] == '\\'))
                    {
                        sb.Append(s[i + 1]);
                        i++;
                        continue;
                    }
                    if (c == quote)
                    {
                        var rest = s.Substring(i + 1).Trim();
                        if (rest.Length > 0 && !rest.StartsWith('#')) return false; // junk after the closing quote
                        value = sb.ToString();
                        sb.Clear();
                        return true;
                    }
                    sb.Append(c);
                }
                sb.Clear();
                return false; // unclosed quote (multi-line values are not supported)
            }

            for (var i = 1; i < s.Length; i++)
            {
                if (s[i] == '#' && char.IsWhiteSpace(s[i - 1]))
                {
                    s = s.Substring(0, i);
                    break;
                }
            }
            value = s.Trim();
            return true;
        }

        private static List<EnvSettingSpec> BuildSettingSpecs()
        {
            var list = new List<EnvSettingSpec>
            {
                // main window (Shared/MainAppSettings.cs)
                new(EnvModule.MainApp, "OPENROUTER_BASE_URL", MainAppSettings.OpenRouterBaseUrlKey, EnvSettingKind.Text, MainAppSettings.DefaultOpenRouterBaseUrl),
                new(EnvModule.MainApp, "OPENROUTER_MODEL", MainAppSettings.OpenRouterModelKey, EnvSettingKind.Text, MainAppSettings.DefaultOpenRouterModel),
            };
            list.AddRange(BackendSettingSpecs.All);
            return list;
        }
    }
}
