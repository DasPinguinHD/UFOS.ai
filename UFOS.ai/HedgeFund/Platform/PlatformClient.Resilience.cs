using System;
using System.Collections.Generic;
using System.Globalization;
using System.Net.Http;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Platform
{
    // =====================================================================================
    // Strategy Platform §10 — robustness on weak connections (contract: backend/strategy_platform/
    // SPEC.md §10.1/§10.3/§10.4): network settings (adaptive or fixed data-loading concurrency),
    // resume of a failed run, auto-resume flag. Same rules as the rest of PlatformClient: session
    // token on every call, never throws, the backend's `error` text is shown verbatim
    // (PlatformResult.Message), no retries and no alternative paths. A 409 on resume carries the
    // backend's reason (wrong error kind, basis changed, holdout run, job slot busy) — shown as sent.
    // =====================================================================================

    internal static partial class PlatformClient
    {
        private static readonly TimeSpan ResumeTimeout = TimeSpan.FromSeconds(20);

        /// <summary>GET /v2/settings/network → {adaptive, fixed, current {alpaca, tiingo}, min, max}.</summary>
        public static Task<PlatformResult<NetworkSettings>> GetNetworkSettingsAsync()
            => SendAsync<NetworkSettings>(HttpMethod.Get, "/v2/settings/network", null, DefaultTimeout);

        /// <summary>PUT /v2/settings/network {adaptive, fixed} — validated (fixed 1–8) and audited by the backend.</summary>
        public static Task<PlatformResult<NetworkSettings>> PutNetworkSettingsAsync(bool adaptive, int fixedConcurrency)
            => SendAsync<NetworkSettings>(HttpMethod.Put, "/v2/settings/network",
                new Dictionary<string, object?> { ["adaptive"] = adaptive, ["fixed"] = fixedConcurrency }, DefaultTimeout);

        /// <summary>POST /v2/backtests/{id}/resume → 202 {run_id} of the NEW run (resumed_from = id); 409 with the reason.</summary>
        public static Task<PlatformResult<BacktestStarted>> ResumeBacktestAsync(string runId)
            => SendAsync<BacktestStarted>(HttpMethod.Post, "/v2/backtests/" + Esc(runId) + "/resume",
                new Dictionary<string, object?>(), ResumeTimeout);

        /// <summary>POST /v2/validations/{id}/resume → 202 {run_id} of the NEW run; 409 with the reason (holdout runs: always 409).</summary>
        public static Task<PlatformResult<BacktestStarted>> ResumeValidationAsync(string runId)
            => SendAsync<BacktestStarted>(HttpMethod.Post, "/v2/validations/" + Esc(runId) + "/resume",
                new Dictionary<string, object?>(), ResumeTimeout);
    }

    /// <summary>GET/PUT /v2/settings/network (SPEC §10.1/§10.4). Nullable on purpose: a missing/null field is
    /// shown as unset, never replaced by a default (no-fallback rule).</summary>
    internal sealed class NetworkSettings
    {
        [JsonPropertyName("adaptive")] public bool? Adaptive { get; set; }
        [JsonPropertyName("fixed")] public int? Fixed { get; set; }
        // {alpaca: n, tiingo: n} — the limiter's concurrency right now (adaptive: moves with timeouts/successes)
        [JsonPropertyName("current")] public JsonElement? Current { get; set; }
        [JsonPropertyName("min")] public int? Min { get; set; }
        [JsonPropertyName("max")] public int? Max { get; set; }
        // data/network.get_settings(): a corrupt stored value is NOT raised — the invalid field is null and these
        // three are added; data requests fail until the settings are saved again (PUT works = the repair path).
        [JsonPropertyName("stored_invalid")] public bool? StoredInvalid { get; set; }
        [JsonPropertyName("error")] public string? Error { get; set; }
        // {"network.adaptive": raw, "network.fixed": raw} — printable raw stored values
        [JsonPropertyName("stored")] public JsonElement? Stored { get; set; }
    }

    /// <summary>Shared texts and rules for waiting / resumable runs (backtests and validations).</summary>
    internal static class RunResume
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        public const string WaitingStatus = "waiting";
        public const int MaxAutoResumes = 5;   // SPEC §10.3 (backend constant; shown in the checkbox text)
        public const string AutoResumeText = "If the connection drops: wait and resume automatically (max 5 times)";
        public const string AutoResumeTooltip =
            "On a connection error (timeout, DNS, unreachable) the run waits instead of failing, checks the connection at most every 30 s "
            + "(no time limit) and continues from its checkpoints. After 5 automatic resumes it fails normally; a manual Resume is still possible. "
            + "A waiting run keeps the single job slot and can be cancelled. A resumed run counts as one trial.";
        public const string ResumeTooltip =
            "Starts a new run from this run's checkpoints (data already loaded, finished steps). Only allowed after a connection error and "
            + "only on an identical basis (same strategy version, request, holdout window, cached data). Counts as the same trial.";

        /// <summary>The run waits for the connection (auto-resume): status "waiting", or a running card whose progress
        /// phase is "waiting" (the run card itself stays "running" while it waits — runcard.mark_waiting).</summary>
        public static bool IsWaiting(string? status, string? phase)
            => status == WaitingStatus || ((status == "running" || status == "queued") && phase == WaitingStatus);

        /// <summary>Resume is offered only when the backend says resumable = true for a FAILED run that is not a
        /// holdout run (a failed holdout run is final, SPEC §10.3). A missing `resumable` never shows the button.</summary>
        public static bool CanResume(string? status, bool? resumable, string? kind)
            => status == "failed" && resumable == true && !string.Equals(kind, "holdout", StringComparison.OrdinalIgnoreCase);

        /// <summary>"resumed from &lt;id&gt; · resume n" or null when the run is not a resume.</summary>
        public static string? ResumedText(string? resumedFrom, int? resumeCount)
        {
            var parts = new List<string>();
            if (!string.IsNullOrWhiteSpace(resumedFrom)) parts.Add("resumed from " + resumedFrom);
            if (resumeCount is int n && n > 0) parts.Add("resume count " + n.ToString(Inv));
            return parts.Count == 0 ? null : string.Join(" · ", parts);
        }

        /// <summary>" (connection error)" etc. for a failed run's error line; empty when the backend sent no kind.</summary>
        public static string ErrorKindSuffix(string? errorKind)
            => string.IsNullOrWhiteSpace(errorKind) ? "" : " (error kind: " + errorKind + ")";

        public static CheckBox MakeAutoResumeCheckBox()
        {
            var box = new CheckBox
            {
                Content = AutoResumeText,
                Foreground = FundPalette.Text,
                FontSize = 11.5,
                IsChecked = false,   // SPEC §10.3: auto_resume default false
                Margin = new Thickness(0, 0, 0, 12),
                ToolTip = AutoResumeTooltip,
            };
            AutomationProperties.SetName(box, AutoResumeText);
            AutomationProperties.SetHelpText(box, AutoResumeTooltip);
            return box;
        }

        public static Button MakeResumeButton(string runId)
        {
            var b = FundUi.MakeButton("↻ Resume", FundPalette.Make(0x267C8CF8), FundPalette.Focus, FundPalette.TextBright);
            b.ToolTip = ResumeTooltip;
            AutomationProperties.SetName(b, "Resume failed run " + runId);
            AutomationProperties.SetHelpText(b, ResumeTooltip);
            return b;
        }

        /// <summary>Calls the resume route; on success returns the new run id. Every failure (409 reason, offline, …)
        /// is shown verbatim in a dialog and null is returned.</summary>
        public static async Task<string?> ResumeAsync(Window? owner, string runId, bool validation)
        {
            var res = validation ? await PlatformClient.ResumeValidationAsync(runId) : await PlatformClient.ResumeBacktestAsync(runId);
            if (res.Ok && !string.IsNullOrWhiteSpace(res.Value?.RunId)) return res.Value!.RunId;
            FundDialogs.Info(owner, "Resume not possible",
                res.Ok ? "Unexpected response from the backend (resume without run_id) — backend and app versions may not match." : res.Message);
            return null;
        }
    }
}
