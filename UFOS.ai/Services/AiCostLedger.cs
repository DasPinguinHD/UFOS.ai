using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Threading;
using UFOS.ai.Logging;

namespace UFOS.ai.Services
{
    // One line of the shared AI cost ledger (see AiCostLedger). Every field except the
    // strings may be missing in a hand-edited / older / foreign line, hence the nullables.
    internal sealed record AiCostEntry(
        DateTimeOffset? Timestamp,
        string Module,
        string Feature,
        string FeatureLabel,
        string Provider,
        string Model,
        decimal? CostUsd,
        int? PromptTokens,
        int? CompletionTokens,
        string Status);

    // Shared, append-only cost ledger for every AI request in UFOS.ai (all modules):
    //   %LOCALAPPDATA%\UFOS.ai\ai-cost-log.jsonl
    // One JSON object per line (UTF-8, "\n"-terminated):
    //   {"ts": ISO-8601 UTC, "module": "UFOS.ai"|"HedgeFund"|"Global Monitoring"|"LiveStreamAgent",
    //    "feature": short id, "feature_label": human label, "provider": "OpenRouter",
    //    "model": model id actually used, "cost_usd": number|null,
    //    "prompt_tokens": int|null, "completion_tokens": int|null, "status": "ok"}
    // The Python backends append their own lines to the same file concurrently, so writes
    // open the file with FileShare.ReadWrite, write each line in one call and retry
    // briefly on sharing violations. Cost = OpenRouter's response "usage.cost" (credits,
    // 1 credit = 1 USD) - what OpenRouter actually charged, not an estimate.
    // Read by TransactionLogWindow. Local only - never uploaded anywhere.
    internal static class AiCostLedger
    {
        public const string FileName = "ai-cost-log.jsonl";

        private static readonly object WriteLock = new();

        public static string DirectoryPath => Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "UFOS.ai");

        public static string LedgerPath => Path.Combine(DirectoryPath, FileName);

        // Appends one entry. Never throws: bookkeeping must not break the AI feature itself.
        public static void Record(
            string module,
            string feature,
            string featureLabel,
            string model,
            decimal? costUsd,
            int? promptTokens,
            int? completionTokens,
            string provider = "OpenRouter",
            string status = "ok")
        {
            try
            {
                byte[] line;
                using (var buffer = new MemoryStream())
                {
                    using (var writer = new Utf8JsonWriter(buffer))
                    {
                        writer.WriteStartObject();
                        writer.WriteString("ts", DateTime.UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss.fff'Z'", CultureInfo.InvariantCulture));
                        writer.WriteString("module", module);
                        writer.WriteString("feature", feature);
                        writer.WriteString("feature_label", featureLabel);
                        writer.WriteString("provider", provider);
                        writer.WriteString("model", model);
                        if (costUsd.HasValue) writer.WriteNumber("cost_usd", costUsd.Value); else writer.WriteNull("cost_usd");
                        if (promptTokens.HasValue) writer.WriteNumber("prompt_tokens", promptTokens.Value); else writer.WriteNull("prompt_tokens");
                        if (completionTokens.HasValue) writer.WriteNumber("completion_tokens", completionTokens.Value); else writer.WriteNull("completion_tokens");
                        writer.WriteString("status", status);
                        writer.WriteEndObject();
                    }
                    buffer.WriteByte((byte)'\n');
                    line = buffer.ToArray();
                }

                lock (WriteLock)
                {
                    Directory.CreateDirectory(DirectoryPath);
                    for (int attempt = 1; ; attempt++)
                    {
                        try
                        {
                            using var stream = new FileStream(LedgerPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite);
                            stream.Write(line, 0, line.Length);
                            stream.Flush();
                            return;
                        }
                        catch (IOException) when (attempt < 6)
                        {
                            // Another process (a Python backend) is appending right now.
                            Thread.Sleep(25 * attempt);
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                try { AppLog.Warn("AiCostLedger", "Could not append to the AI cost ledger", ex); } catch { }
            }
        }

        // All readable entries in file order (oldest first). Tolerance rules:
        //  - missing file -> empty list; file locked -> short retries, then empty list;
        //  - blank lines, non-JSON lines and JSON that isn't an object are skipped;
        //  - missing/null/wrong-typed fields fall back to defaults instead of dropping the line
        //    (strings -> "", model -> "unknown", provider -> "OpenRouter", status -> "ok",
        //    numbers -> null, unparsable ts -> null);
        //  - numbers are also accepted as numeric strings; tokens also as 1512.0;
        //    ts also as Unix seconds.
        // Never throws.
        public static List<AiCostEntry> ReadAll()
        {
            var entries = new List<AiCostEntry>();
            try
            {
                if (!File.Exists(LedgerPath))
                {
                    return entries;
                }

                string content = string.Empty;
                for (int attempt = 1; ; attempt++)
                {
                    try
                    {
                        using var stream = new FileStream(LedgerPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
                        using var reader = new StreamReader(stream, Encoding.UTF8, detectEncodingFromByteOrderMarks: true);
                        content = reader.ReadToEnd();
                        break;
                    }
                    catch (IOException) when (attempt < 6)
                    {
                        Thread.Sleep(25 * attempt);
                    }
                }

                foreach (var rawLine in content.Split('\n'))
                {
                    var line = rawLine.Trim();
                    if (line.Length == 0)
                    {
                        continue;
                    }

                    var entry = TryParseLine(line);
                    if (entry is not null)
                    {
                        entries.Add(entry);
                    }
                }
            }
            catch (Exception ex)
            {
                try { AppLog.Warn("AiCostLedger", "Could not read the AI cost ledger", ex); } catch { }
            }
            return entries;
        }

        // "$0.0123" (below one cent: 4 decimals, otherwise 3), "—" when unknown.
        public static string FormatUsd(decimal? amount)
        {
            if (amount is not decimal value)
            {
                return "—";
            }
            var format = Math.Abs(value) < 0.01m ? "0.0000" : "#,##0.000";
            return "$" + value.ToString(format, CultureInfo.InvariantCulture);
        }

        public static string FormatTokens(int? tokens) =>
            tokens is int t ? t.ToString("N0", CultureInfo.InvariantCulture) : "—";

        private static AiCostEntry? TryParseLine(string line)
        {
            try
            {
                using var doc = JsonDocument.Parse(line);
                var root = doc.RootElement;
                if (root.ValueKind != JsonValueKind.Object)
                {
                    return null;
                }

                var feature = GetString(root, "feature") ?? string.Empty;
                return new AiCostEntry(
                    Timestamp: GetTimestamp(root, "ts"),
                    Module: GetString(root, "module") ?? string.Empty,
                    Feature: feature,
                    FeatureLabel: GetString(root, "feature_label") ?? feature,
                    Provider: GetString(root, "provider") ?? "OpenRouter",
                    Model: GetString(root, "model") ?? "unknown",
                    CostUsd: GetDecimal(root, "cost_usd"),
                    PromptTokens: GetInt(root, "prompt_tokens"),
                    CompletionTokens: GetInt(root, "completion_tokens"),
                    Status: GetString(root, "status") ?? "ok");
            }
            catch (JsonException)
            {
                return null;
            }
        }

        private static string? GetString(JsonElement root, string name)
        {
            if (!root.TryGetProperty(name, out var value))
            {
                return null;
            }
            return value.ValueKind switch
            {
                JsonValueKind.String => string.IsNullOrWhiteSpace(value.GetString()) ? null : value.GetString()!.Trim(),
                JsonValueKind.Number => value.GetRawText(),
                _ => null,
            };
        }

        internal static decimal? GetDecimal(JsonElement root, string name)
        {
            if (!root.TryGetProperty(name, out var value))
            {
                return null;
            }
            if (value.ValueKind == JsonValueKind.Number)
            {
                if (value.TryGetDecimal(out var d)) return d;
                if (value.TryGetDouble(out var dbl) && !double.IsNaN(dbl) && !double.IsInfinity(dbl))
                {
                    try { return (decimal)dbl; } catch (OverflowException) { return null; }
                }
                return null;
            }
            if (value.ValueKind == JsonValueKind.String &&
                decimal.TryParse(value.GetString(), NumberStyles.Float, CultureInfo.InvariantCulture, out var parsed))
            {
                return parsed;
            }
            return null;
        }

        internal static int? GetInt(JsonElement root, string name)
        {
            if (!root.TryGetProperty(name, out var value))
            {
                return null;
            }
            if (value.ValueKind == JsonValueKind.Number)
            {
                if (value.TryGetInt32(out var i)) return i;
                if (value.TryGetDouble(out var dbl) && dbl >= 0 && dbl <= int.MaxValue && Math.Floor(dbl) == dbl) return (int)dbl;
                return null;
            }
            if (value.ValueKind == JsonValueKind.String &&
                int.TryParse(value.GetString(), NumberStyles.Integer, CultureInfo.InvariantCulture, out var parsed))
            {
                return parsed;
            }
            return null;
        }

        private static DateTimeOffset? GetTimestamp(JsonElement root, string name)
        {
            if (!root.TryGetProperty(name, out var value))
            {
                return null;
            }
            if (value.ValueKind == JsonValueKind.String &&
                DateTimeOffset.TryParse(value.GetString(), CultureInfo.InvariantCulture,
                    DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal, out var ts))
            {
                return ts;
            }
            if (value.ValueKind == JsonValueKind.Number && value.TryGetDouble(out var seconds) &&
                seconds > 0 && seconds < 253402300799)
            {
                return DateTimeOffset.FromUnixTimeMilliseconds((long)(seconds * 1000));
            }
            return null;
        }
    }
}
