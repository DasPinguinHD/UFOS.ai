using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net.Http;
using System.Text.Json;
using System.Text.Json.Nodes;
using UFOS.ai.Logging;

namespace UFOS.ai.Shared
{
    /// <summary>A paid AI call refused by a cap BEFORE it was made (fail closed). Message = user-facing text.</summary>
    internal sealed class AiBudgetBlockedException : Exception
    {
        public AiBudgetBlockedException(string key, string reason, string message) : base(message)
        {
            Key = key;
            Reason = reason;
        }

        public string Key { get; }
        /// <summary>off | reached | unreadable | unrecorded | invalid | store (contract §3).</summary>
        public string Reason { get; }
    }

    /// <summary>
    /// App-local AI caps (contract UFOS.ai/docs/ai-budgets-spec.md §5 step 2b / §10.1): the main app's
    /// "Summarize with AI" (Services/OpenRouterClient.cs) has no backend, so its daily cap is enforced
    /// in-process — reserve BEFORE the call, settle with OpenRouter's usage.cost afterwards — and stored in
    /// %LOCALAPPDATA%\UFOS.ai\ai-budgets-app.json (never .env):
    ///   {"schema": 1, "limits": {"article_summary": 1.0}, "spend": {"article_summary": {"day": "2026-10-17", "usd": 0.0123}}}
    /// Registered as the app-local provider "UFOS.ai app" in AiBudgetProviders; the AI budgets window
    /// reads/writes it through the same schema-1 payload / PUT result as a backend.
    ///
    /// Fail closed: a missing file = first use (defaults, spent 0); an unreadable/corrupt file blocks the
    /// calls and refuses edits (fix or rename the file — writing it would reset today's spend); a failed
    /// spend write pauses the calls until the app restarts; an invalid stored limit blocks the calls until
    /// it is set again. Reservations are process-local (only the main app makes these calls).
    /// Linked into the module projects too (Shared/*.cs) but only used by the main app — BCL types only.
    /// </summary>
    internal sealed class AiBudgetAppCaps : IAiBudgetProvider, IAiBudgetLocalSource
    {
        public const string SummaryKey = "article_summary";
        public const string FileName = "ai-budgets-app.json";
        private const string LogModule = "AI budgets";
        private const string Where = "AI budgets (main window → Settings)";

        private sealed record CapDef(string Key, string Label, string Group, decimal DefaultUsd, decimal MinUsd,
                                     decimal MaxUsd, decimal Step, decimal EstimateUsd, string Notes,
                                     string CostLogModule, string CostLogFeature);

        private static readonly CapDef[] Caps =
        {
            new(SummaryKey, "Article · Summarize with AI", "Main app", 1.00m, 0m, 50m, 0.1m, 0.01m,
                "Checked in the app before every ✦ Summarize with AI click (estimate $0.01 reserved; OpenRouter's reported cost is booked afterwards).",
                "UFOS.ai", "article-summary"),
        };

        private readonly object _lock = new();
        private readonly Dictionary<string, decimal> _inFlight = new(StringComparer.Ordinal);
        private bool _unrecorded;

        public static AiBudgetAppCaps Instance { get; } = new();

        private AiBudgetAppCaps() { }

        // ===================== IAiBudgetProvider =====================

        public string ModuleId => "ufos-app";
        public string Label => "UFOS.ai app";
        public string[] BackendRelativeDir { get; } = Array.Empty<string>();
        public int Port => 0;
        public IReadOnlyList<string> CostLogModules { get; } = new[] { "UFOS.ai" };
        public string StartHint => string.Empty;

        public static string StatePath => Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "UFOS.ai", FileName);

        // ===================== Enforcement =====================

        /// <summary>A reserved estimate; pass it to exactly one of Settle / Release / ChargeForException.</summary>
        internal sealed class Reservation
        {
            internal Reservation(string key, decimal estimate, string day)
            {
                Key = key;
                Estimate = estimate;
                Day = day;
            }

            public string Key { get; }
            public decimal Estimate { get; }
            public string Day { get; }
            internal bool Done { get; set; }
        }

        /// <summary>BEFORE the call: reserves the estimate or throws <see cref="AiBudgetBlockedException"/>.</summary>
        public Reservation Reserve(string key)
        {
            var cap = Cap(key);
            lock (_lock)
            {
                var day = UtcDay();
                var state = ReadStateOrBlock(cap);
                var limit = LimitOrBlock(cap, state);
                if (limit <= 0m) throw Blocked(cap, "off");
                if (!TrySpentOn(state, key, day, out var spent)) throw Blocked(cap, "unreadable");
                _inFlight.TryGetValue(key, out var inFlight);
                if (spent + inFlight + cap.EstimateUsd > limit) throw Blocked(cap, "reached", spent, limit);
                _inFlight[key] = inFlight + cap.EstimateUsd;
                return new Reservation(key, cap.EstimateUsd, day);
            }
        }

        /// <summary>After a completed call: books OpenRouter's reported cost (no reported cost → the estimate).</summary>
        public void Settle(Reservation? res, decimal? costUsd)
        {
            if (res is null) return;
            var amount = costUsd is decimal c && c >= 0m ? c : res.Estimate;
            Finish(res, amount);
        }

        /// <summary>The call never left / was rejected by the provider: books nothing.</summary>
        public void Release(Reservation? res)
        {
            if (res is null) return;
            Finish(res, null);
        }

        /// <summary>
        /// A failed call: timeouts, broken/unparsable responses and other failures after sending book the
        /// estimate (the provider may bill them); an HTTP error status (request rejected, e.g. 402/429/5xx)
        /// and connection/name-resolution failures (never left) book nothing.
        /// </summary>
        public void ChargeForException(Reservation? res, Exception ex)
        {
            if (res is null) return;
            if (ex is HttpRequestException hre &&
                (hre.StatusCode is not null ||
                 hre.HttpRequestError is HttpRequestError.ConnectionError or HttpRequestError.NameResolutionError
                                      or HttpRequestError.ProxyTunnelError or HttpRequestError.SecureConnectionError))
            {
                Release(res);
                return;
            }
            Finish(res, res.Estimate);
        }

        private void Finish(Reservation res, decimal? book)
        {
            lock (_lock)
            {
                if (res.Done) return;
                res.Done = true;
                if (_inFlight.TryGetValue(res.Key, out var inFlight))
                {
                    var rest = inFlight - res.Estimate;
                    if (rest <= 0m) _inFlight.Remove(res.Key);
                    else _inFlight[res.Key] = rest;
                }
                if (book is not decimal amount || amount <= 0m) return;
                try
                {
                    var state = ReadState() ?? throw new InvalidDataException("the AI budget file is unreadable");
                    if (state["spend"] is not JsonObject spend)
                    {
                        spend = new JsonObject();
                        state["spend"] = spend;
                    }
                    var current = spend[res.Key] as JsonObject;
                    var storedDay = current?["day"] is JsonValue dv && dv.TryGetValue<string>(out var sd) ? sd : null;
                    if (storedDay is not null && string.CompareOrdinal(storedDay, res.Day) > 0)
                    {
                        return;   // the call started yesterday (UTC); today's counter is not touched
                    }
                    if (!TrySpentOn(state, res.Key, res.Day, out var usd))
                        throw new InvalidDataException("today's stored spend is not a valid number");
                    spend[res.Key] = new JsonObject { ["day"] = res.Day, ["usd"] = Math.Round(usd + amount, 6) };
                    WriteState(state);
                }
                catch (Exception ex)
                {
                    _unrecorded = true;
                    try { AppLog.Error(LogModule, $"AI spend of {res.Key} could not be recorded — app-side AI calls are paused until the app restarts", ex); } catch { }
                }
            }
        }

        // ===================== IAiBudgetLocalSource (window) =====================

        public AiBudgetPayload LocalPayload()
        {
            var now = DateTimeOffset.UtcNow;
            var day = now.UtcDateTime.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);
            var budgets = new List<AiBudgetEntry>();
            lock (_lock)
            {
                var state = ReadState();
                foreach (var cap in Caps) budgets.Add(Entry(cap, state, day));
            }
            return new AiBudgetPayload
            {
                Schema = 1,
                Module = ModuleId,
                Label = Label,
                GeneratedAt = now.ToString("yyyy-MM-dd'T'HH:mm:ss'+00:00'", CultureInfo.InvariantCulture),
                Day = day,
                Month = now.UtcDateTime.ToString("yyyy-MM", CultureInfo.InvariantCulture),
                Budgets = budgets,
            };
        }

        public AiBudgetPutResult LocalPut(string key, decimal limitUsd)
        {
            CapDef? cap = Array.Find(Caps, c => c.Key == key);
            if (cap is null) return AiBudgetPutResult.Failure(400, "unknown budget key");
            if (limitUsd < cap.MinUsd || limitUsd > cap.MaxUsd)
                return AiBudgetPutResult.Failure(400, $"a number {cap.MinUsd.ToString("0.##", CultureInfo.InvariantCulture)}..{cap.MaxUsd.ToString("0.##", CultureInfo.InvariantCulture)} (0 = off)");
            var value = Math.Round(limitUsd, 6);
            decimal? old;
            lock (_lock)
            {
                var state = ReadState();
                if (state is null)
                {
                    return AiBudgetPutResult.Failure(503,
                        $"The app's AI budget file is unreadable ({StatePath}) — fix or rename it, then try again (see the Backend Log).");
                }
                old = TryLimit(cap, state, out var stored) ? stored : null;
                if (state["limits"] is not JsonObject limits)
                {
                    limits = new JsonObject();
                    state["limits"] = limits;
                }
                limits[key] = value;
                try
                {
                    WriteState(state);
                }
                catch (Exception ex)
                {
                    try { AppLog.Error(LogModule, $"AI budget {key} could not be stored", ex); } catch { }
                    return AiBudgetPutResult.Failure(503, "The budget could not be stored (the app's AI budget file could not be written) — see the Backend Log.");
                }
            }
            try { AppLog.Info(LogModule, $"AI budget changed: {ModuleId}/{key} {old?.ToString(CultureInfo.InvariantCulture) ?? "invalid"} -> {value.ToString(CultureInfo.InvariantCulture)} (by the app)"); } catch { }
            var payload = LocalPayload();
            payload.Changed = new AiBudgetChange { Key = key, Old = old, New = value };
            return AiBudgetPutResult.Success(payload);
        }

        private AiBudgetEntry Entry(CapDef cap, JsonObject? state, string day)
        {
            var e = new AiBudgetEntry
            {
                Key = cap.Key,
                Label = cap.Label,
                Group = cap.Group,
                Period = "day",
                PeriodId = day,
                DefaultUsd = cap.DefaultUsd,
                Min = cap.MinUsd,
                Max = cap.MaxUsd,
                Step = cap.Step,
                Status = "ok",
                Editable = true,
                Applies = "immediately",
                Notes = cap.Notes,
                CostLog = new List<AiBudgetCostLogRef> { new() { Module = cap.CostLogModule, Feature = cap.CostLogFeature } },
            };
            if (state is null)
            {
                e.Status = "error";
                e.Editable = false;
                e.Message = Message(cap, "unreadable") + $" Fix or rename {StatePath}.";
                return e;
            }
            if (!TryLimit(cap, state, out var limit))
            {
                e.Status = "error";
                e.Message = Message(cap, "invalid");
                return e;
            }
            e.LimitUsd = limit;
            e.Enabled = limit > 0m;
            if (!TrySpentOn(state, cap.Key, day, out var spent))
            {
                e.Status = "error";
                e.Message = Message(cap, "unreadable") + $" Fix or rename {StatePath}.";
                return e;
            }
            _inFlight.TryGetValue(cap.Key, out var inFlight);
            e.SpentUsd = Math.Round(spent, 6);
            e.RemainingUsd = Math.Round(Math.Max(0m, limit - spent), 6);
            if (_unrecorded)
            {
                e.Status = "paused";
                e.Message = Message(cap, "unrecorded");
            }
            else if (limit <= 0m)
            {
                e.Status = "blocked";
                e.Message = Message(cap, "off");
            }
            else if (spent + inFlight + cap.EstimateUsd > limit)
            {
                e.Status = "blocked";
                e.Message = Message(cap, "reached", spent, limit);
            }
            return e;
        }

        // ===================== State file =====================

        private static CapDef Cap(string key) =>
            Array.Find(Caps, c => c.Key == key) ?? throw new ArgumentException("unknown app AI budget key: " + key, nameof(key));

        private static string UtcDay() => DateTime.UtcNow.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);

        private JsonObject ReadStateOrBlock(CapDef cap)
        {
            if (_unrecorded) throw Blocked(cap, "unrecorded");
            return ReadState() ?? throw Blocked(cap, "unreadable");
        }

        private static decimal LimitOrBlock(CapDef cap, JsonObject state) =>
            TryLimit(cap, state, out var limit) ? limit : throw Blocked(cap, "invalid");

        /// <summary>Stored limit (missing → default); false for a stored value that is not a number in range.</summary>
        private static bool TryLimit(CapDef cap, JsonObject state, out decimal limit)
        {
            limit = cap.DefaultUsd;
            if (state["limits"] is not JsonObject limits || !limits.ContainsKey(cap.Key)) return true;
            if (limits[cap.Key] is JsonValue v && v.TryGetValue<decimal>(out var stored) && stored >= cap.MinUsd && stored <= cap.MaxUsd)
            {
                limit = stored;
                return true;
            }
            return false;
        }

        /// <summary>Today's stored spend (none stored / another day → 0); false when today's entry is corrupt.</summary>
        private static bool TrySpentOn(JsonObject state, string key, string day, out decimal spent)
        {
            spent = 0m;
            if (!state.ContainsKey("spend")) return true;
            if (state["spend"] is not JsonObject spend) return false;
            if (!spend.ContainsKey(key)) return true;
            if (spend[key] is not JsonObject s || s["day"] is not JsonValue d || !d.TryGetValue<string>(out var storedDay)) return false;
            if (storedDay != day) return true;
            if (s["usd"] is JsonValue u && u.TryGetValue<decimal>(out var usd) && usd >= 0m)
            {
                spent = usd;
                return true;
            }
            return false;
        }

        /// <summary>The state object; a missing file is a fresh state; null = unreadable/corrupt (fail closed).</summary>
        private static JsonObject? ReadState()
        {
            try
            {
                var path = StatePath;
                if (!File.Exists(path)) return new JsonObject { ["schema"] = 1 };
                var info = new FileInfo(path);
                if (info.Length > 64 * 1024) throw new InvalidDataException("the AI budget file is larger than 64 KB");
                using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
                var node = JsonNode.Parse(fs);
                if (node is not JsonObject obj) throw new InvalidDataException("the AI budget file is not a JSON object");
                if (obj["schema"] is not JsonValue sv || !sv.TryGetValue<int>(out var schema) || schema != 1)
                    throw new InvalidDataException("the AI budget file has an unknown schema");
                return obj;
            }
            catch (Exception ex)
            {
                try { AppLog.Warn(LogModule, $"The app's AI budget file {StatePath} could not be read — app-side AI calls are blocked", ex); } catch { }
                return null;
            }
        }

        /// <summary>Atomic write (temp file + replace). Throws on failure.</summary>
        private static void WriteState(JsonObject state)
        {
            var path = StatePath;
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            state["schema"] = 1;
            var tmp = path + ".tmp";
            File.WriteAllText(tmp, state.ToJsonString(new JsonSerializerOptions { WriteIndented = true }));
            File.Move(tmp, path, overwrite: true);
        }

        // ===================== Messages (contract §3 wording) =====================

        private static AiBudgetBlockedException Blocked(CapDef cap, string reason, decimal spent = 0m, decimal limit = 0m) =>
            new(cap.Key, reason, Message(cap, reason, spent, limit));

        private static string Usd(decimal v) =>
            v >= 0.995m || v == 0m ? v.ToString("0.00", CultureInfo.InvariantCulture) : v.ToString("0.0000", CultureInfo.InvariantCulture);

        private static string Message(CapDef cap, string reason, decimal spent = 0m, decimal limit = 0m) => reason switch
        {
            "off" => $"{cap.Label} is switched off (daily cap is 0) — raise it in {Where}.",
            "reached" => $"Daily AI cap for {cap.Label} reached (${Usd(spent)} of ${Usd(limit)} today, UTC) — raise it in {Where} or try again tomorrow.",
            "unreadable" => $"{cap.Label}: today's AI spend could not be read — AI calls are paused until it can be read again (see the Backend Log).",
            "unrecorded" => $"{cap.Label}: AI spend could not be recorded — AI calls are paused until the app restarts.",
            "invalid" => $"The stored daily cap for {cap.Label} is invalid — set it again in {Where}.",
            _ => $"{cap.Label}: the app's AI budget store is unavailable — AI calls are paused (see the Backend Log).",
        };
    }
}
