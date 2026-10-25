using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.Logging;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// Hedge Fund → "⚙ Keys &amp; data" (Strategy Platform stage 1; contract
    /// backend/strategy_platform/SPEC.md §2 and §4). Proof of concept, paper trading only.
    ///
    /// Keys: one row per credential of EVERY module (CredentialStore.Secrets = Shared/CredentialCatalog.cs — S10-E: keys live
    /// only in the Windows Credential Manager, no module reads a .env). The status comes from the Credential Manager directly
    /// (works without any backend); the Hedge Fund backend's GET /v2/settings/keys adds its read errors for its keys.
    /// "Set…" writes via CredentialStore (the Alpaca key ID and secret are entered together), "Remove" asks first,
    /// "Import from old .env files…" (KeysAndDataWindow.EnvMigration.cs) is the one-time migration — the ONLY code that reads
    /// a .env. Every change is reported to POST /v2/settings/keys/changed for the
    /// audit log — name and action only, NEVER the value; when the backend isn't running the
    /// change still happens locally and the window says it was not recorded.
    ///
    /// Data sources: GET /v2/data/sources as a table, "Test Alpaca connection" → POST
    /// /v2/data/test. Without the backend this section only says so (status, no fallback).
    ///
    /// Network (SPEC §10.1/§10.4, KeysAndDataWindow.Network.cs): adaptive or fixed data-loading
    /// concurrency via GET/PUT /v2/settings/network, current concurrency per source.
    ///
    /// Brokers (SPEC §18, KeysAndDataWindow.Brokers.cs) — this window is "Settings → Brokers": internal paper,
    /// Alpaca paper and Binance Spot demo (status, test, connect, reconciliation + Resolve…, routing, per-broker
    /// kill, read-only live guard; the Kraken Futures demo is retired — its stored keys are listed only for removal). The Binance demo key and secret are entered together
    /// like the Alpaca pair.
    ///
    /// Chrome: borderless like the other Hedge Fund windows — full-screen padding/square
    /// corners in Window_StateChanged, drag-restore out of full screen (CLAUDE.md).
    /// </summary>
    public partial class KeysAndDataWindow : Window
    {
        private const string LogModule = "HedgeFund";

        private Dictionary<string, KeyStatus> _backendKeys = new(StringComparer.Ordinal);
        private int _keysGeneration;
        private int _sourcesGeneration;
        private bool _keyActionRunning;

        public KeysAndDataWindow()
        {
            InitializeComponent();
            // settings window: NOT started maximized (user request 2026-10-23); a manual maximize stays within the work area
            UFOS.ai.Shared.WindowMaximize.ConstrainToWorkArea(this);
            // retest 2026-10-23: closing this owned borderless window returned focus to the MainWindow — activate the owner instead
            Closing += (_, e) => { if (!e.Cancel) UFOS.ai.HedgeFund.Fund.FundUi.ActivateOwnerOnClose(this); };
            InitBrokers();
            InitOptionsArchive();   // OS1: Data sources → options archive card
            InitAutoRefresh();      // user 2026-10-24: the whole window refreshes itself (10 s while settling / backend down, else 60 s)
        }

        private async void Window_Loaded(object sender, RoutedEventArgs e)
        {
            RenderKeys();
            try
            {
                await Task.WhenAll(RefreshBackendKeysAsync(), RefreshSourcesAsync(), RefreshNetworkAsync(), RefreshBrokersAsync(force: true));
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Keys & data window: initial refresh failed.", ex);
            }
        }

        // =====================================================================================
        // Keys
        // =====================================================================================

        /// <param name="quiet">auto refresh: no "checking…" flicker; the result replaces the text when it arrives.</param>
        private async Task RefreshBackendKeysAsync(bool quiet = false)
        {
            var generation = ++_keysGeneration;
            if (!quiet) KeysBackendText.Text = "Backend: checking…";
            var result = await PlatformClient.GetKeysAsync();
            if (generation != _keysGeneration) return;

            if (result.Ok && result.Value?.Keys != null)
            {
                _backendKeys = result.Value.Keys
                    .Where(k => !string.IsNullOrEmpty(k.Name))
                    .GroupBy(k => k.Name!, StringComparer.Ordinal)
                    .ToDictionary(g => g.Key, g => g.Last(), StringComparer.Ordinal);
                KeysBackendText.Text = "Backend: connected. It reads the keys when it needs them, so a change applies without a restart.";
            }
            else
            {
                _backendKeys = new Dictionary<string, KeyStatus>(StringComparer.Ordinal);
                KeysBackendText.Text = result.Offline
                    ? "Hedge Fund backend not running — keys can still be set and removed here. The audit log needs the backend."
                    : "Backend key status unavailable: " + result.Message;
            }
            RenderKeys();
            await RefreshEodhdBudgetAsync(generation);
        }

        /// <summary>Stage 9d (SPEC §20.11): the EODHD daily call counter (used / left) as the backend reports it — never computed here.</summary>
        private async Task RefreshEodhdBudgetAsync(int generation)
        {
            var res = await PlatformClient.GetShadowPriceSourcesAsync();
            if (generation != _keysGeneration) return;
            string text;
            if (!res.Ok || res.Value == null)
                text = "EODHD calls today: not available (" + (res.Ok ? "the backend sent no price sources" : PlatformClient.DescribeFailure(res)) + ")";
            else if (res.Value.Sources?.FirstOrDefault(x => x.Id == "eodhd") is ShPriceSource eod)
                text = ShMapText.Budget(eod.Budget) + (eod.KeyPresent == false ? " · EODHD API key not set" : "")
                       + (string.IsNullOrWhiteSpace(eod.Terms) ? "" : " · " + eod.Terms);
            else
                text = "EODHD calls today: not available (the backend reports no EODHD source)";
            EodhdBudgetText.Text = text;
            AutomationProperties.SetName(EodhdBudgetText, text);
        }

        private void RenderKeys()
        {
            KeysRowsPanel.Children.Clear();
            foreach (var secret in CredentialStore.Secrets)
            {
                KeysRowsPanel.Children.Add(BuildKeyRow(secret));
            }
            // Stage 10 (S10-K): a key of a retired integration is listed only while it is still stored — "Remove" is its only action
            foreach (var secret in CredentialStore.RetiredSecrets)
            {
                KeyState? state;
                try { state = CredentialStore.GetState(secret.Name); }
                catch (CredentialStoreException) { state = KeyState.Unreadable; }   // shown so it can be removed
                if (state == KeyState.Set || state == KeyState.Unreadable) KeysRowsPanel.Children.Add(BuildRetiredKeyRow(secret));
            }
        }

        /// <summary>Stage 10 (S10-K): a stored key of a retired integration (Kraken demo) — not used by anything; can only be removed.</summary>
        private Border BuildRetiredKeyRow(SecretInfo secret)
        {
            var grid = new Grid();
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var info = new StackPanel { Margin = new Thickness(0, 0, 12, 0) };
            info.Children.Add(FundUi.Text(secret.Label, 12, FundPalette.Dim));
            var target = FundUi.Text(CredentialStore.Target(secret.Name), 10.5, FundPalette.Faint, mono: true, wrap: false);
            target.Margin = new Thickness(0, 2, 0, 0);
            info.Children.Add(target);
            var note = FundUi.Text("Kraken Futures demo retired 2026-10-19 — this stored key is not used by anything; remove it.", 10, FundPalette.Risk);
            note.Margin = new Thickness(0, 2, 0, 0);
            info.Children.Add(note);
            grid.Children.Add(info);
            var removeButton = FundUi.MakeButton("Remove", FundPalette.ButtonBg, FundPalette.ButtonBorder, FundPalette.Kill);
            removeButton.FontSize = 11.5;
            removeButton.Padding = new Thickness(10, 3, 10, 3);
            removeButton.VerticalAlignment = VerticalAlignment.Center;
            removeButton.IsEnabled = !_keyActionRunning;
            AutomationProperties.SetName(removeButton, "Remove " + secret.Label);
            removeButton.Click += async (_, _) => await RunKeyActionSafeAsync(() => RemoveKeyAsync(secret), "remove");
            Grid.SetColumn(removeButton, 1);
            grid.Children.Add(removeButton);
            return new Border
            {
                BorderBrush = FundPalette.Hairline,
                BorderThickness = new Thickness(0, 0, 0, 1),
                Padding = new Thickness(0, 9, 0, 9),
                Child = grid,
            };
        }

        private Border BuildKeyRow(SecretInfo secret)
        {
            KeyState? state;
            string? localError = null;
            try
            {
                state = CredentialStore.GetState(secret.Name);
            }
            catch (CredentialStoreException ex)
            {
                state = null;
                localError = ex.Message;
            }
            bool? isSet = state == null ? null : state == KeyState.Set;
            var unreadable = state == KeyState.Unreadable;
            _backendKeys.TryGetValue(secret.Name, out var backend);

            var grid = new Grid();
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(250) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

            // Column 0: label, credential target, which modules use it
            var info = new StackPanel { Margin = new Thickness(0, 0, 12, 0) };
            info.Children.Add(FundUi.Text(secret.Label, 12, FundPalette.TextBright));
            var target = FundUi.Text(CredentialStore.Target(secret.Name), 10.5, FundPalette.Faint, mono: true, wrap: false);
            target.Margin = new Thickness(0, 2, 0, 0);
            info.Children.Add(target);
            var usedBy = FundUi.Text("Used by: " + secret.UsedBy, 10, FundPalette.Faint);
            usedBy.Margin = new Thickness(0, 2, 0, 0);
            info.Children.Add(usedBy);
            grid.Children.Add(info);

            // Column 1: status
            string statusText;
            Brush statusBrush;
            if (localError != null)
            {
                statusText = "Credential Manager error";
                statusBrush = FundPalette.Kill;
            }
            else if (unreadable)
            {
                // Stored, but not as UTF-8 text (e.g. UTF-16 from cmdkey / the Credential Manager UI).
                statusText = "unreadable — set it again";
                statusBrush = FundPalette.Kill;
            }
            else if (isSet == true)
            {
                statusText = "● Set";
                statusBrush = FundPalette.Long;
            }
            else
            {
                statusText = "Not set";
                statusBrush = FundPalette.Dim;
            }
            var status = new StackPanel { VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 12, 0) };
            status.Children.Add(FundUi.Text(statusText, 11.5, statusBrush, bold: isSet == true || unreadable || localError != null));
            var notes = new List<string>();
            if (localError != null) notes.Add(localError);
            if (unreadable) notes.Add("The stored value is not UTF-8 text (e.g. written by cmdkey or the Credential Manager UI) — the backend can't use it. Use Set… or Remove.");
            if (!string.IsNullOrWhiteSpace(backend?.Error)) notes.Add("Backend: " + backend!.Error);
            else if (isSet == true && backend?.Present == false)
                notes.Add("The backend can't see it — is it running under another Windows user?");
            foreach (var note in notes)
            {
                var n = FundUi.Text(note, 10, FundPalette.Faint);
                n.Margin = new Thickness(0, 2, 0, 0);
                status.Children.Add(n);
            }
            Grid.SetColumn(status, 1);
            grid.Children.Add(status);

            // Column 2: actions
            var actions = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
            var setButton = FundUi.MakeButton("Set…");
            setButton.FontSize = 11.5;
            setButton.Padding = new Thickness(10, 3, 10, 3);
            setButton.IsEnabled = !_keyActionRunning;
            AutomationProperties.SetName(setButton, "Set " + secret.Label);
            setButton.ToolTip = IsAlpaca(secret)
                ? "Enter the Alpaca paper API key ID and secret key together."
                : IsBinance(secret)
                ? "Enter the Binance Spot DEMO API key and secret together — created at demo.binance.com → API Management (system-generated / HMAC)."
                : "Enter a new value. It is stored in the Windows Credential Manager, never shown again.";
            setButton.Click += async (_, _) => await RunKeyActionSafeAsync(() => SetKeyAsync(secret), "set");
            actions.Children.Add(setButton);

            var removeButton = FundUi.MakeButton("Remove", FundPalette.ButtonBg, FundPalette.ButtonBorder, FundPalette.Kill);
            removeButton.FontSize = 11.5;
            removeButton.Padding = new Thickness(10, 3, 10, 3);
            removeButton.Margin = new Thickness(8, 0, 0, 0);
            removeButton.IsEnabled = !_keyActionRunning && (isSet == true || unreadable);
            AutomationProperties.SetName(removeButton, "Remove " + secret.Label);
            removeButton.Click += async (_, _) => await RunKeyActionSafeAsync(() => RemoveKeyAsync(secret), "remove");
            actions.Children.Add(removeButton);
            Grid.SetColumn(actions, 2);
            grid.Children.Add(actions);

            return new Border
            {
                BorderBrush = FundPalette.Hairline,
                BorderThickness = new Thickness(0, 0, 0, 1),
                Padding = new Thickness(0, 9, 0, 9),
                Child = grid,
            };
        }

        /// <summary>Button handlers must never crash the app: unexpected errors are logged and shown (no value involved).</summary>
        private async Task RunKeyActionSafeAsync(Func<Task> action, string what)
        {
            try
            {
                await action();
            }
            catch (Exception ex)
            {
                _keyActionRunning = false;
                AppLog.Warn(LogModule, "Keys & data: key " + what + " failed unexpectedly.", ex);
                ShowKeysMessage("The key could not be " + (what == "set" ? "saved" : "removed") + " (" + ex.GetType().Name + "). Press ↻ Refresh to see the current state.", FundPalette.Kill);
                RenderKeys();
            }
        }

        private static bool IsAlpaca(SecretInfo secret)
            => secret.Name == CredentialStore.AlpacaKeyId || secret.Name == CredentialStore.AlpacaSecretKey;

        /// <summary>Stage 8b (SPEC §18.14): the Binance Spot demo key pair (HMAC), entered together like the Alpaca pair.</summary>
        private static bool IsBinance(SecretInfo secret)
            => secret.Name == CredentialStore.BinanceDemoApiKey || secret.Name == CredentialStore.BinanceDemoApiSecret;

        /// <summary>Format checks for a value before it is stored. The message never contains the value.</summary>
        private static string? Validate(SecretInfo secret, string value)
        {
            var trimmed = (value ?? "").Trim();
            if (trimmed.Length == 0) return secret.Label + ": please enter a value.";
            if (trimmed.Any(char.IsControl)) return secret.Label + ": contains line breaks or control characters.";
            var bytes = Encoding.UTF8.GetByteCount(trimmed);
            if (bytes > CredentialStore.MaxBlobBytes)
                return secret.Label + $": too long ({bytes} bytes; the Credential Manager allows at most {CredentialStore.MaxBlobBytes}).";
            // §18.14: the backend accepts HMAC keys only — a PEM private key (RSA / Ed25519) would be refused at every call
            if (secret.Name == CredentialStore.BinanceDemoApiSecret && trimmed.Contains("PRIVATE KEY", StringComparison.Ordinal))
                return secret.Label + ": HMAC keys only — create a system-generated (HMAC) API key at demo.binance.com → API Management.";
            if (secret.Name == "sec-edgar-user-agent" && !trimmed.Contains('@'))
                return "SEC EDGAR asks for a name and an e-mail address, e.g. \"Jane Doe jane@example.com\".";
            return null;
        }

        private async Task SetKeyAsync(SecretInfo secret)
        {
            if (_keyActionRunning) return;
            var targets = IsAlpaca(secret)
                ? new[] { CredentialStore.Find(CredentialStore.AlpacaKeyId)!, CredentialStore.Find(CredentialStore.AlpacaSecretKey)! }
                : IsBinance(secret)
                ? new[] { CredentialStore.Find(CredentialStore.BinanceDemoApiKey)!, CredentialStore.Find(CredentialStore.BinanceDemoApiSecret)! }
                : new[] { secret };

            string[]? values;
            try
            {
                var fields = targets.Select(t => new KeyField(t.Label, t.IsSecretValue)).ToList();
                var binance = IsBinance(secret);
                var message = targets.Length > 1
                    ? (binance
                        ? "Enter the API key and API secret of your Binance Spot DEMO account (log in at binance.com → Demo Trading → API Management → "
                          + "Create API → system-generated (HMAC); spot trading on the DEMO only — never keys of a real Binance account). Both are stored "
                          + "in the Windows Credential Manager."
                        : "Enter the key ID and secret key of your Alpaca PAPER trading account. Both are stored in the Windows Credential Manager.")
                    : "Enter the new value. It is stored in the Windows Credential Manager and replaces any value stored before.";
                var title = targets.Length > 1
                    ? (binance ? "Set Binance Spot demo keys" : "Set Alpaca paper keys")
                    : "Set " + secret.Label;
                var dialog = new KeyEntryDialog(this, title, message, fields,
                    vals =>
                    {
                        for (var i = 0; i < targets.Length; i++)
                        {
                            var problem = Validate(targets[i], vals[i]);
                            if (problem != null) return problem;
                        }
                        return null;
                    },
                    "Values are never shown again, logged or sent to the backend.");
                values = dialog.ShowDialog() == true ? dialog.TakeValues() : null;
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Keys & data: the key entry dialog could not be shown.", ex);
                ShowKeysMessage("The input dialog could not be shown — nothing was changed. See the Backend Log.", FundPalette.Kill);
                return;
            }
            if (values == null) return;

            _keyActionRunning = true;
            var saved = new List<SecretInfo>();
            string? failure = null;
            try
            {
                for (var i = 0; i < targets.Length; i++)
                {
                    try
                    {
                        CredentialStore.Set(targets[i].Name, values[i]);
                        saved.Add(targets[i]);
                        AppLog.Info(LogModule, "Key stored in the Windows Credential Manager: " + targets[i].Name);
                    }
                    catch (Exception ex) when (ex is CredentialStoreException || ex is ArgumentException)
                    {
                        failure = targets[i].Label + " was NOT saved: " + ex.Message;
                        break;
                    }
                }
            }
            finally
            {
                Array.Clear(values);
            }

            try
            {
                var auditProblems = await ReportChangesAsync(saved, "set");
                var text = saved.Count > 0 ? "Saved: " + string.Join(", ", saved.Select(s => s.Label)) + "." : "";
                if (failure != null) text = (text + " " + failure).Trim();
                if (auditProblems != null) text += " " + auditProblems;
                ShowKeysMessage(text, failure != null ? FundPalette.Kill : auditProblems != null ? FundPalette.Risk : FundPalette.Long);
            }
            finally
            {
                _keyActionRunning = false;
                RenderKeys();
            }
            await RefreshBackendKeysAsync();
        }

        private async Task RemoveKeyAsync(SecretInfo secret)
        {
            if (_keyActionRunning) return;
            var confirmed = FundDialogs.Confirm(this, "Remove key",
                "Remove \"" + secret.Label + "\" from the Windows Credential Manager?\n\n"
                + "Functions that need it stop with a \"key missing\" message until you set it again.",
                "Remove", danger: true);
            if (!confirmed) return;

            _keyActionRunning = true;
            try
            {
                bool existed;
                try
                {
                    existed = CredentialStore.Remove(secret.Name);
                }
                catch (CredentialStoreException ex)
                {
                    ShowKeysMessage(ex.Message, FundPalette.Kill);
                    return;
                }
                if (!existed)
                {
                    ShowKeysMessage("\"" + secret.Label + "\" was not stored — nothing to remove.", FundPalette.Dim);
                    return;
                }
                AppLog.Info(LogModule, "Key removed from the Windows Credential Manager: " + secret.Name);
                var auditProblems = await ReportChangesAsync(new[] { secret }, "removed");
                ShowKeysMessage("Removed: " + secret.Label + "." + (auditProblems != null ? " " + auditProblems : ""),
                    auditProblems != null ? FundPalette.Risk : FundPalette.Long);
            }
            finally
            {
                _keyActionRunning = false;
                RenderKeys();
            }
            await RefreshBackendKeysAsync();
        }

        private async void ImportEnvButton_Click(object sender, RoutedEventArgs e)
        {
            if (_keyActionRunning) return;
            _keyActionRunning = true;
            ImportEnvButton.IsEnabled = false;
            try
            {
                // S10-E: the one-time migration (EnvMigration.cs) — the only code that reads a .env. Names only on screen.
                var plan = EnvMigration.BuildPlan();
                var dialog = new EnvMigrationDialog(this, plan);
                dialog.ShowDialog();
                if (!dialog.Applied)
                {
                    ShowKeysMessage("Import from old .env files: cancelled — nothing was changed.", FundPalette.Dim);
                }
                else
                {
                    var imported = plan.Keys.Where(k => k.Imported).Select(k => k.Secret).ToList();
                    foreach (var k in plan.Keys)
                        AppLog.Info(LogModule, "Import from old .env files: " + k.Secret.Name + " (" + k.Secret.UsedBy + ") — " + k.Outcome);
                    foreach (var s in plan.Settings)
                        AppLog.Info(LogModule, "Import from old .env files: setting " + s.Spec.EnvName + " (" + EnvMigration.ModuleName(s.Spec.Module) + ") — " + s.Outcome);
                    foreach (var f in plan.FileOutcomes) AppLog.Info(LogModule, "Import from old .env files: " + f);

                    var auditProblems = await ReportChangesAsync(imported, "imported");
                    var failed = plan.Keys.Count(k => k.Failed) + plan.Settings.Count(s => s.Failed) + (plan.AnyFileProblem ? 1 : 0);
                    var text = $"Import from old .env files: {imported.Count} key{(imported.Count == 1 ? "" : "s")} imported"
                               + (failed > 0 ? $", {failed} problem{(failed == 1 ? "" : "s")} (see the import result / app log)" : "") + "."
                               + (auditProblems != null ? " " + auditProblems : "");
                    ShowKeysMessage(text, failed > 0 || auditProblems != null ? FundPalette.Risk : FundPalette.Long);
                }
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Keys & data: import from old .env files failed.", ex);
                ShowKeysMessage("Import from old .env files failed (" + ex.GetType().Name + "). Press ↻ Refresh to see which keys are set.", FundPalette.Kill);
            }
            finally
            {
                _keyActionRunning = false;
                ImportEnvButton.IsEnabled = true;
                RenderKeys();
            }
            await RefreshBackendKeysAsync();
        }

        /// <summary>
        /// POST /v2/settings/keys/changed for each changed key (name + action only). Null when all
        /// were recorded, else one sentence saying which were not — the local change stands.
        /// </summary>
        private static async Task<string?> ReportChangesAsync(IEnumerable<SecretInfo> changed, string action)
        {
            var missed = new List<string>();
            string? reason = null;
            foreach (var secret in changed)
            {
                var result = await PlatformClient.ReportKeyChangedAsync(secret.Name, action);
                if (!result.Ok)
                {
                    missed.Add(secret.Label);
                    reason ??= result.Offline ? "backend not running" : result.Message;
                }
            }
            if (missed.Count == 0) return null;
            AppLog.Warn(LogModule, "Key change (" + action + ") not recorded in the backend audit log: " + reason);
            return "Not recorded in the backend audit log (" + reason + "): " + string.Join(", ", missed) + ".";
        }

        private void ShowKeysMessage(string text, Brush brush)
        {
            KeysMessageText.Text = text;
            KeysMessageText.Foreground = brush;
            KeysMessageBorder.Visibility = string.IsNullOrWhiteSpace(text) ? Visibility.Collapsed : Visibility.Visible;
        }

        private async void RefreshKeysButton_Click(object sender, RoutedEventArgs e)
        {
            try
            {
                RenderKeys();
                await RefreshBackendKeysAsync();
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Keys & data: key refresh failed.", ex);
            }
        }

        // =====================================================================================
        // Data sources
        // =====================================================================================

        /// <param name="quiet">auto refresh: no "Loading…" flicker; the result replaces the text when it arrives.</param>
        private async Task RefreshSourcesAsync(bool quiet = false)
        {
            var generation = ++_sourcesGeneration;
            if (!quiet)
            {
                SourcesStatusText.Text = "Loading…";
                SourcesStatusText.Foreground = FundPalette.Dim;
            }
            var result = await PlatformClient.GetSourcesAsync();
            if (generation != _sourcesGeneration) return;

            if (!result.Ok)
            {
                SourcesGrid.Children.Clear();
                SourcesGrid.RowDefinitions.Clear();
                SourcesStatusText.Text = result.Offline ? PlatformClient.BackendNotRunning : result.Message;
                SourcesStatusText.Foreground = FundPalette.Risk;
                return;
            }

            var sources = result.Value?.Sources ?? new List<DataSourceStatus>();
            SourcesStatusText.Text = sources.Count == 0
                ? "The backend reports no data sources."
                : sources.Count + " data type" + (sources.Count == 1 ? "" : "s") + " · updated " + DateTime.Now.ToString("HH:mm:ss");
            SourcesStatusText.Foreground = FundPalette.Dim;
            RenderSources(sources);
        }

        private void RenderSources(IReadOnlyList<DataSourceStatus> sources)
        {
            var grid = SourcesGrid;
            grid.Children.Clear();
            grid.RowDefinitions.Clear();
            grid.ColumnDefinitions.Clear();
            foreach (var width in new[] { 1.1, 0.8, 0.7, 1.0, 2.2, 1.8 })
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(width, GridUnitType.Star) });

            void AddCell(int row, int column, TextBlock text)
            {
                text.Margin = new Thickness(0, 5, 10, 5);
                Grid.SetRow(text, row);
                Grid.SetColumn(text, column);
                grid.Children.Add(text);
            }

            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            var headers = new[] { "Data type", "Source", "Status", "Last OK", "Message", "Licence" };
            for (var c = 0; c < headers.Length; c++)
                AddCell(0, c, FundUi.Text(headers[c], 10.5, FundPalette.Faint, bold: true, wrap: false));

            var row = 1;
            foreach (var s in sources)
            {
                grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                var line = new Border { BorderBrush = FundPalette.Hairline, BorderThickness = new Thickness(0, 1, 0, 0) };
                Grid.SetRow(line, row);
                Grid.SetColumnSpan(line, headers.Length);
                grid.Children.Add(line);

                var status = (s.Status ?? "").Trim().ToLowerInvariant();
                var (statusText, statusBrush) = status switch
                {
                    "ok" => ("OK", FundPalette.Long),
                    "error" => ("Error", FundPalette.Kill),
                    "" => ("not checked yet", FundPalette.Faint),
                    _ => (s.Status!, FundPalette.Dim),
                };
                var message = s.Message ?? "";
                if (status == "error" && !string.IsNullOrWhiteSpace(s.LastErrorAt))
                    message = (message + " (" + FundFormat.LocalTime(s.LastErrorAt, "yyyy-MM-dd HH:mm") + ")").Trim();

                AddCell(row, 0, FundUi.Text(FundFormat.Label(s.DataType), 11.5, FundPalette.Text));
                AddCell(row, 1, FundUi.Text(s.Source ?? "—", 11.5, FundPalette.Text, mono: true));
                AddCell(row, 2, FundUi.Text(statusText, 11.5, statusBrush, bold: status == "ok" || status == "error"));
                AddCell(row, 3, FundUi.Text(FundFormat.LocalTime(s.LastOkAt, "yyyy-MM-dd HH:mm"), 11, FundPalette.Dim, mono: true));
                AddCell(row, 4, FundUi.Text(string.IsNullOrWhiteSpace(message) ? "—" : message, 11, FundPalette.Dim));
                var licence = FundUi.Text(s.LicenceText, 10.5, FundPalette.Faint);
                if (string.Equals((s.Source ?? "").Trim(), "fred", StringComparison.OrdinalIgnoreCase))
                {
                    // FRED API terms: the required notice + a link to the terms, under the licence text of the FRED row
                    licence.Inlines.Add(new System.Windows.Documents.LineBreak());
                    licence.Inlines.Add(new System.Windows.Documents.Run(UFOS.ai.HedgeFund.Newsroom.FredAttribution.Notice + " "));
                    licence.Inlines.Add(UFOS.ai.HedgeFund.Newsroom.FredAttribution.CreateLink(FundPalette.Focus));
                    AutomationProperties.SetName(licence, s.LicenceText + ". " + UFOS.ai.HedgeFund.Newsroom.FredAttribution.Notice
                                                          + " Link: " + UFOS.ai.HedgeFund.Newsroom.FredAttribution.TermsLinkText + ".");
                }
                AddCell(row, 5, licence);
                row++;
            }
        }

        private async void RefreshSourcesButton_Click(object sender, RoutedEventArgs e)
        {
            try
            {
                await RefreshSourcesAsync();
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Keys & data: data source refresh failed.", ex);
            }
        }

        private async void TestAlpacaButton_Click(object sender, RoutedEventArgs e)
        {
            TestAlpacaButton.IsEnabled = false;
            TestAlpacaButton.Content = "Testing…";
            TestResultsPanel.Visibility = Visibility.Visible;
            TestResultsPanel.Children.Clear();
            TestResultsPanel.Children.Add(FundUi.Text("Testing… (SPY and BTC/USD daily bar, latest SPY quote — can take up to about 2 minutes when the backend's rate limit makes it wait)", 11.5, FundPalette.Dim));
            try
            {
                var result = await PlatformClient.TestSourceAsync("alpaca");
                TestResultsPanel.Children.Clear();
                if (!result.Ok || result.Value == null)
                {
                    var text = result.Ok ? "The backend sent an empty test result." : result.Offline ? PlatformClient.BackendNotRunning : result.Message;
                    TestResultsPanel.Children.Add(FundUi.Text("✕ Test not run: " + text, 11.5, FundPalette.Kill));
                }
                else
                {
                    var test = result.Value;
                    TestResultsPanel.Children.Add(FundUi.Text(
                        test.Ok ? "✓ Alpaca connection OK" : "✕ Alpaca connection test failed",
                        12, test.Ok ? FundPalette.Long : FundPalette.Kill, bold: true));
                    foreach (var check in test.Checks ?? new List<DataTestCheck>())
                    {
                        var line = FundUi.Text((check.Ok ? "✓ " : "✕ ") + (check.Name ?? "check")
                                               + (string.IsNullOrWhiteSpace(check.Message) ? "" : " — " + check.Message),
                                               11.5, check.Ok ? FundPalette.Text : FundPalette.Kill);
                        line.Margin = new Thickness(12, 4, 0, 0);
                        TestResultsPanel.Children.Add(line);
                    }
                    var stamp = FundUi.Text("Tested at " + DateTime.Now.ToString("HH:mm:ss"), 10, FundPalette.Faint);
                    stamp.Margin = new Thickness(0, 6, 0, 0);
                    TestResultsPanel.Children.Add(stamp);
                }
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Keys & data: Alpaca connection test failed.", ex);
                TestResultsPanel.Children.Clear();
                TestResultsPanel.Children.Add(FundUi.Text("✕ The test could not be run (" + ex.GetType().Name + ").", 11.5, FundPalette.Kill));
            }
            finally
            {
                TestAlpacaButton.Content = "Test Alpaca connection";
                TestAlpacaButton.IsEnabled = true;
            }
            try { await RefreshSourcesAsync(); }
            catch (Exception ex) { AppLog.Warn(LogModule, "Keys & data: data source refresh failed.", ex); }
        }

        // =====================================================================================
        // Window chrome (same pattern as HedgeFundWindow / FinancialNewsroomWindow)
        // =====================================================================================

        private bool _restoreOnDrag;
        private Point _restoreDragStart;

        private void TitleBar_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
        {
            if (e.LeftButton != MouseButtonState.Pressed) return;
            if (e.ClickCount == 2)
            {
                WindowState = WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized;
                return;
            }
            if (WindowState == WindowState.Maximized)
            {
                _restoreOnDrag = true;
                _restoreDragStart = e.GetPosition(this);
                ((UIElement)sender).CaptureMouse();
                return;
            }
            try { DragMove(); } catch { }
        }

        private void TitleBar_MouseMove(object sender, MouseEventArgs e)
        {
            if (!_restoreOnDrag || e.LeftButton != MouseButtonState.Pressed) return;
            var pos = e.GetPosition(this);
            if (Math.Abs(pos.X - _restoreDragStart.X) < SystemParameters.MinimumHorizontalDragDistance &&
                Math.Abs(pos.Y - _restoreDragStart.Y) < SystemParameters.MinimumVerticalDragDistance) return;

            _restoreOnDrag = false;
            ((UIElement)sender).ReleaseMouseCapture();

            var ratioX = ActualWidth > 0 ? pos.X / ActualWidth : 0.5;
            var restoreWidth = RestoreBounds.Width;
            // Cursor position in screen DIPs (PointToScreen returns device pixels).
            var toDip = PresentationSource.FromVisual(this)?.CompositionTarget?.TransformFromDevice ?? Matrix.Identity;
            var cursor = toDip.Transform(PointToScreen(pos));

            WindowState = WindowState.Normal;
            Left = cursor.X - restoreWidth * ratioX;
            Top = cursor.Y - 9; // middle of the 18 px title bar
            try { DragMove(); } catch { }
        }

        private void TitleBar_MouseLeftButtonUp(object sender, MouseButtonEventArgs e)
        {
            if (!_restoreOnDrag) return;
            _restoreOnDrag = false;
            ((UIElement)sender).ReleaseMouseCapture();
        }

        // Maximized borderless window: pad by the resize frame (+ padded border) so the title
        // bar isn't cut off, and use square corners; restore both when normal.
        private void Window_StateChanged(object? sender, EventArgs e)
        {
            if (WindowState == WindowState.Maximized)
            {
                // 2026-10-23: the WM_GETMINMAXINFO hook (Shared/WindowMaximize.cs) fits the maximized window to the work area —
                // no frame padding (the old padding left a gap and the window covered the taskbar)
                RootBorder.Margin = new Thickness(0);
                RootBorder.CornerRadius = new CornerRadius(0);
                TitleBarBorder.CornerRadius = new CornerRadius(0);
            }
            else
            {
                RootBorder.Margin = new Thickness(0);
                RootBorder.CornerRadius = new CornerRadius(8);
                TitleBarBorder.CornerRadius = new CornerRadius(8, 8, 0, 0);
            }
        }

        private void MinimizeButton_Click(object sender, RoutedEventArgs e)
        {
            WindowState = WindowState.Minimized;
        }

        private void CloseButton_Click(object sender, RoutedEventArgs e)
        {
            Close();
        }
    }
}
