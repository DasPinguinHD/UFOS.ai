using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// The user's decision on a strategy PAUSED by the decay monitor (SPEC §19.2): "Keep running…" or "Demote…" — one
    /// confirmation step with a precise button text and an OPTIONAL audit note (fix1023, 2026-10-23: no tick, no typed DEMOTE,
    /// no confirm word sent). Shows why it was paused (rule, the numbers of the pause, the live numbers, the backend's reason
    /// verbatim) and the consequences. The request is sent from the dialog; the backend's answer (400 / 409 not_paused) is shown
    /// inline, verbatim. Nothing is sent before the user confirms; the backend writes the audit row (decay_decision) first.
    /// </summary>
    internal sealed class MonDecisionDialog : FundPopupWindow
    {
        private readonly MonStrategy _strategy;
        private readonly string _action;
        private readonly TextBox _reason;
        private readonly TextBlock _error;
        private readonly Button _ok;
        private bool _busy;
        private bool _closed;

        /// <summary>The backend's answer on success, else null.</summary>
        public MonDecisionResult? Result { get; private set; }

        public MonDecisionDialog(Window? owner, MonStrategy s, string action, int? minSessions)
            : base(owner, action == PlatformClient.DecisionDemote ? "Demote strategy" : "Keep running", 640, 640, resizable: true)
        {
            _strategy = s;
            _action = action;
            var demote = action == PlatformClient.DecisionDemote;
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text((demote ? "Demote " : "Keep running ") + s.DisplayName + "?", 14, FundPalette.TextBright, bold: true));
            stack.Children.Add(MonUi.Line(s.StrategyId + " · v" + (s.Version?.ToString(CultureInfo.InvariantCulture) ?? "?") + " · run " + (s.RunId ?? "?") + " · account " + (s.AccountId ?? "?"),
                                          10.5, FundPalette.Faint, mono: true));

            // why it was paused
            var p = s.Decay?.Paused;
            var rule = (s.Decay?.Rules ?? new List<MonRule>()).FirstOrDefault(x => x.Rule == p?.Rule);
            stack.Children.Add(TradingUi.Head("Why it was paused"));
            stack.Children.Add(MonUi.Line("Rule: " + MonUi.RuleName(p?.Rule) + " · paused " + TradingText.Ts(p?.At) + " · check day " + (p?.CheckDay ?? "—"), 11.5, FundPalette.Text));
            if (!string.IsNullOrWhiteSpace(p?.Reason)) stack.Children.Add(MonUi.Line(p!.Reason, 11.5, FundPalette.Text));
            // fix1023 B2: the numbers of THIS pause (labelled synthetic for a sandbox force-decay); live numbers only for a real pause
            if (MonUi.PauseNumbersText(p) is string pauseNumbers)
                stack.Children.Add(MonUi.Line(pauseNumbers, 11, MonUi.IsSyntheticPause(p) ? FundPalette.Kill : FundPalette.Text));
            if (rule != null && !MonUi.IsSyntheticPause(p))
                stack.Children.Add(MonUi.Line("Now: value " + MonUi.Frac(rule.Value, 2) + " vs threshold " + MonUi.Frac(rule.Threshold, 2) + " · breached "
                                              + (rule.Count?.ToString(CultureInfo.InvariantCulture) ?? "—") + " of " + (rule.Needed?.ToString(CultureInfo.InvariantCulture) ?? "—"),
                                              11, FundPalette.Text, mono: true));
            var r = s.Realised;
            var d = s.Deviation;
            if (r != null && d != null)
                stack.Children.Add(MonUi.Line("Realised " + MonUi.LogAsPct(r.CumLogReturn) + " vs band " + MonUi.LogAsPct(d.Band?.Lo) + " … " + MonUi.LogAsPct(d.Band?.Hi)
                                              + " · z " + MonUi.Num(d.ZCum) + " · rolling z " + MonUi.Num(d.RollingZ) + " · drawdown " + MonUi.Frac(r.Drawdown, 2)
                                              + " (holdout max " + MonUi.Frac(s.Expected?.MaxDrawdown, 2) + ") · vol ratio " + MonUi.Num(d.VolRatio), 10.5, FundPalette.Dim, mono: true));

            stack.Children.Add(TradingUi.Head("What happens"));
            if (demote)
            {
                stack.Children.Add(TradingUi.Callout("The incubation run is ABORTED and the strategy goes back to \"validated\" (the same path as \"Abort run…\"). Its positions are then "
                                                     + "closed by the runtime in the next tradable window, like any ended source. A new incubation needs a new run, with its length "
                                                     + "chosen again. This cannot be undone.", FundPalette.Kill, FundPalette.KillSurface));
            }
            else
            {
                var min = minSessions is int v ? v.ToString(CultureInfo.InvariantCulture) + " sessions"
                                                : "MIN_SESSIONS sessions (not available: the decay rules could not be read from the backend)";
                stack.Children.Add(TradingUi.Callout("The pause ends at once: the strategy sends orders again in its next execution window (paper / demo). From then on ONLY the "
                                                     + "returns after your decision count (§19.14): the return and volatility rules need a fresh sample of " + min + " "
                                                     + "before they can fire again, and the drawdown is measured from the decision. The expected band stays frozen.",
                                                     FundPalette.Risk, MonUi.RiskSurface));
            }
            stack.Children.Add(TradingUi.Label(TradingUi.NoteLabel));
            _reason = TradingUi.NoteBox("Optional note for: " + (demote ? "demote " : "keep running ") + s.DisplayName);
            _reason.TextChanged += (_, _) => Update();
            stack.Children.Add(_reason);
            _error = TradingUi.ErrorText();
            stack.Children.Add(_error);
            var okText = demote
                ? "Demote — abort run " + (s.RunId ?? "?") + " and close positions"
                : "Keep running — end the pause, orders resume";
            _ok = demote ? TradingUi.Danger(okText) : TradingUi.Primary(okText);
            _ok.IsEnabled = TradingUi.ValidNote(_reason.Text);
            AutomationProperties.SetName(_ok, okText);
            stack.Children.Add(TradingUi.Buttons(out var cancel, _ok));
            cancel.Click += (_, _) => { if (!_closed && !_busy) DialogResult = false; };
            _ok.Click += async (_, _) => await SubmitAsync();
            Closed += (_, _) => _closed = true;
            SetBody(new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = stack });
            Loaded += (_, _) => _reason.Focus();
        }

        private bool Valid() => TradingUi.ValidNote(_reason.Text);

        private void Update() => _ok.IsEnabled = !_busy && Valid();

        private async Task SubmitAsync()
        {
            if (_busy || !Valid() || string.IsNullOrEmpty(_strategy.StrategyId)) return;
            _busy = true;
            TradingUi.SetBusy(_ok, true, _action == PlatformClient.DecisionDemote ? "Demoting…" : "Sending…");
            Update();
            TradingUi.ShowError(_error, "Sending…");
            _error.Foreground = FundPalette.Faint;
            PlatformResult<MonDecisionResult> res;
            try { res = await PlatformClient.DecideDecayAsync(_strategy.StrategyId!, _action, _reason.Text.Trim()); }
            finally { _busy = false; TradingUi.SetBusy(_ok, false); }
            if (_closed) return;
            _error.Foreground = FundPalette.Kill;
            if (res.Ok && res.Value != null)
            {
                Result = res.Value;
                DialogResult = true;
                return;
            }
            TradingUi.ShowError(_error, res.Ok ? "Unexpected response from the backend (decision)." : PlatformClient.DescribeFailure(res));
            Update();
        }
    }
}
