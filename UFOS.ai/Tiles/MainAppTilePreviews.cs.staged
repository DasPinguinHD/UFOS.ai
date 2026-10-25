using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Media;
using UFOS.ai.Logging;
using UFOS.ai.ModuleTiles;
using UFOS.ai.Services;

namespace UFOS.ai.Tiles
{
    /// <summary>
    /// Transaction Log tile (TILES_API §3 + §10.3): AI spend today / this month from the local ledger
    /// %LOCALAPPDATA%\UFOS.ai\ai-cost-log.jsonl (Services/AiCostLedger) — no backend, no network.
    /// "vs budget" only when EVERY cap can be read without a running backend: the backends' daily caps live in the backends
    /// (GET /ai/budgets) — so it is never complete offline and the tile shows the spend + "budgets: see AI budgets" (no guessed totals).
    /// </summary>
    public sealed class TransactionLogTilePreview : IModuleTilePreview
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        public string ModuleId => "transaction-log";
        public string DisplayName => "Transaction Log";
        public string? LiveUnavailableReason => null;
        public string? LastViewUnavailableReason => null;

        public Task<TileLiveResult> ReadLiveAsync(CancellationToken ct) => Task.Run(() =>
        {
            if (!File.Exists(AiCostLedger.LedgerPath)) return TileLiveResult.NoData("no AI request recorded yet");
            var entries = AiCostLedger.ReadAll();   // logs a read problem itself (System Logs) and returns what it could read
            var now = DateTimeOffset.Now;
            decimal today = 0, month = 0;
            var withoutCost = 0;
            var todayCount = 0;
            foreach (var e in entries)
            {
                if (e.Timestamp is not DateTimeOffset ts) continue;
                var local = ts.ToLocalTime();
                if (local.Year != now.Year || local.Month != now.Month) continue;
                if (e.CostUsd is not decimal c)
                {
                    withoutCost++;
                    continue;
                }
                month += c;
                if (local.Day == now.Day)
                {
                    today += c;
                    todayCount++;
                }
            }
            var figures = new List<TileFigure>
            {
                new("AI spend today", AiCostLedger.FormatUsd(today), TileTone.Neutral, todayCount.ToString(Inv) + " request(s) · budgets: see AI budgets"),
                new("This month", AiCostLedger.FormatUsd(month), TileTone.Neutral,
                    withoutCost > 0 ? withoutCost.ToString(Inv) + " request(s) without reported cost" : null),
            };
            return TileLiveResult.Fresh(new TileLiveData(now, figures));
        }, ct);

        public Drawing CreateIllustration()
        {
            var g = TileArt.Canvas();
            for (var i = 0; i < 5; i++)
            {
                var y = 14 + i * 14;
                g.Children.Add(TileArt.Box("ControlBackgroundBrush", 20, y, 120, 9, 3));
                g.Children.Add(TileArt.Box(i % 2 == 0 ? "AccentBlueDimBrush" : "AiAccentBrush", 112, y + 2, 22, 5, 2, 0.8));
            }
            g.Children.Add(TileArt.Dot("PositiveGreenBrush", 26, 18, 2));
            return g;
        }

        public bool OwnsWindow(Window window) => window is TransactionLogWindow;
        public bool IsSandbox(Window window) => false;
    }

    /// <summary>
    /// System Logs tile (TILES_API §10.9): errors / warnings of the last 24 h in the app log (%LOCALAPPDATA%\UFOS.ai\logs\app.log,
    /// all modules' AppLog) and each backend's data/logs/backend.log (+ its rotated .1–.3) — local files only. The LiveStream
    /// Agent backend writes no backend.log (console + event files only); that is named in the detail line, not hidden.
    /// Lines are parsed by their timestamp prefix; per file the warning/error lines are cached by (size, write time).
    /// </summary>
    public sealed class SystemLogsTilePreview : IModuleTilePreview
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        // app.log: "[2026-10-24 17:05:00.123] [WARN ] [Module] message" (local time, UFOS.ai.Logging.LogEntry)
        private static readonly Regex AppLine = new(@"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d{3}\] \[(WARN|ERROR)\s*\]", RegexOptions.Compiled | RegexOptions.CultureInvariant);
        // backend.log: "2026-10-24 17:05:00,123 WARNING name: message" (local time, Python logging)
        private static readonly Regex PyLine = new(@"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} (WARNING|ERROR|CRITICAL) ", RegexOptions.Compiled | RegexOptions.CultureInvariant);

        private readonly Dictionary<string, (long Length, DateTime WriteUtc, List<(DateTime Ts, bool Error)> Lines)> _cache = new(StringComparer.OrdinalIgnoreCase);
        private readonly object _sync = new();

        public string ModuleId => "system-logs";
        public string DisplayName => "System Logs";
        public string? LiveUnavailableReason => null;
        public string? LastViewUnavailableReason => null;

        public Task<TileLiveResult> ReadLiveAsync(CancellationToken ct) => Task.Run(() =>
        {
            var since = DateTime.Now.AddHours(-24);
            var files = new List<(string Path, bool Python, string Name)> { (AppLog.LogFilePath, false, "app") };
            foreach (var (rel, name) in new[] { (new[] { "HedgeFund", "backend" }, "Hedge Fund"), (new[] { "Global Monitoring", "backend" }, "Global Monitoring") })
            {
                var dir = UFOS.ai.Shared.BackendAuth.FindBackendDir(rel);
                if (dir == null) continue;
                var log = Path.Combine(dir, "data", "logs", "backend.log");
                files.Add((log, true, name));
                for (var i = 1; i <= 3; i++) files.Add((log + "." + i.ToString(Inv), true, name));
            }
            int warnings = 0, errors = 0;
            var unreadable = new List<string>();
            lock (_sync)
            {
                foreach (var (path, python, name) in files)
                {
                    ct.ThrowIfCancellationRequested();
                    try
                    {
                        if (!File.Exists(path)) continue;
                        var info = new FileInfo(path);
                        if (info.LastWriteTime < since) continue;   // nothing of the last 24 h in it
                        if (!_cache.TryGetValue(path, out var c) || c.Length != info.Length || c.WriteUtc != info.LastWriteTimeUtc)
                        {
                            c = (info.Length, info.LastWriteTimeUtc, Parse(path, python));
                            _cache[path] = c;
                        }
                        foreach (var (ts, isError) in c.Lines)
                        {
                            if (ts < since) continue;
                            if (isError) errors++; else warnings++;
                        }
                    }
                    catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
                    {
                        if (!unreadable.Contains(name)) unreadable.Add(name);
                    }
                }
            }
            var detail = "app log + Hedge Fund / Global Monitoring backend logs · LiveStream Agent backend keeps no log file";
            var figures = new List<TileFigure>
            {
                new("Errors (24 h)", errors.ToString("N0", Inv), errors > 0 ? TileTone.Negative : TileTone.Neutral),
                new("Warnings (24 h)", warnings.ToString("N0", Inv), warnings > 0 ? TileTone.Caution : TileTone.Neutral,
                    unreadable.Count > 0 ? "not readable: " + string.Join(", ", unreadable) + " log" : detail),
            };
            return TileLiveResult.Fresh(new TileLiveData(DateTimeOffset.Now, figures));
        }, ct);

        private static List<(DateTime, bool)> Parse(string path, bool python)
        {
            var list = new List<(DateTime, bool)>();
            using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            using var reader = new StreamReader(fs);
            string? line;
            while ((line = reader.ReadLine()) != null)
            {
                var m = (python ? PyLine : AppLine).Match(line);
                if (!m.Success) continue;
                if (!DateTime.TryParseExact(m.Groups[1].Value, "yyyy-MM-dd HH:mm:ss", Inv, DateTimeStyles.AssumeLocal, out var ts)) continue;
                var level = m.Groups[2].Value;
                list.Add((ts, level == "ERROR" || level == "CRITICAL"));
            }
            return list;
        }

        public Drawing CreateIllustration()
        {
            var g = TileArt.Canvas();
            for (var i = 0; i < 6; i++)
            {
                var y = 12 + i * 12;
                var w = 60 + (i * 37 % 60);
                g.Children.Add(TileArt.Box("ControlBackgroundBrush", 20, y, w, 6, 2));
                if (i == 2) g.Children.Add(TileArt.Dot("CautionBrush", 14, y + 3, 2.2));
                if (i == 4) g.Children.Add(TileArt.Dot("AccentRedBrush", 14, y + 3, 2.2));
            }
            return g;
        }

        // the System Logs window is LiveStreamAgent's BackendLogsWindow (title "Backend Log", built in code)
        public bool OwnsWindow(Window window) => window.GetType() == typeof(Window) && window.Title == "Backend Log";
        public bool IsSandbox(Window window) => false;
    }

    /// <summary>Sentiment Optimization / Strategy Discovery: not built yet — Live and Last view are disabled in the menu with
    /// the reason; the tile shows the Illustration (the menu says why, not a silent fallback).</summary>
    public sealed class NotBuiltTilePreview : IModuleTilePreview
    {
        public const string Reason = "module not built yet";
        private readonly bool _sentiment;

        public NotBuiltTilePreview(string moduleId, string displayName)
        {
            ModuleId = TilePaths.CheckId(moduleId);
            DisplayName = displayName;
            _sentiment = moduleId == "sentiment-optimization";
        }

        public string ModuleId { get; }
        public string DisplayName { get; }
        public string? LiveUnavailableReason => Reason;
        public string? LastViewUnavailableReason => Reason;
        public Task<TileLiveResult> ReadLiveAsync(CancellationToken ct) => Task.FromResult(TileLiveResult.NotBuilt(Reason));

        public Drawing CreateIllustration()
        {
            var g = TileArt.Canvas();
            if (_sentiment)
            {
                g.Children.Add(TileArt.Polyline("AiAccentBrush", 2, 0.9, new Point(16, 60), new Point(40, 44), new Point(62, 52), new Point(86, 30), new Point(110, 38), new Point(144, 22)));
                g.Children.Add(TileArt.Polyline("AccentBlueDimBrush", 1.5, 0.7, new Point(16, 70), new Point(48, 62), new Point(80, 66), new Point(112, 54), new Point(144, 58)));
                g.Children.Add(TileArt.Dot("AiAccentBrush", 86, 30, 3));
            }
            else
            {
                for (var r = 0; r < 3; r++)
                    for (var c = 0; c < 5; c++)
                        g.Children.Add(TileArt.Box(r == 1 && c == 3 ? "PositiveGreenDimBrush" : "ControlBackgroundBrush", 22 + c * 24, 18 + r * 20, 18, 14, 3));
                g.Children.Add(TileArt.Ring("PositiveGreenBrush", 103, 45, 12, 1.5, 0.8));
            }
            return g;
        }

        public bool OwnsWindow(Window window) => false;
        public bool IsSandbox(Window window) => false;
    }
}
