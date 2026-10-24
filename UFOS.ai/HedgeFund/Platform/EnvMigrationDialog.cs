using System;
using System.Collections.Generic;
using System.Linq;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Media;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// "Import from old .env files…" (⚙ Keys &amp; data, S10-E): ONE dialog that shows the plan by NAME only (never a value),
    /// takes the choices (keep / replace a stored key, which file's value, keep an unknown name in its file), applies it on
    /// "Import, verify and remove" and then shows the result in place. The values stay inside <see cref="EnvMigrationPlan"/>
    /// and are dropped when the dialog closes (<see cref="EnvMigrationPlan.Forget"/>). Logic: EnvMigration.cs.
    /// </summary>
    internal sealed class EnvMigrationDialog : FundPopupWindow
    {
        private readonly EnvMigrationPlan _plan;
        private readonly TextBlock _error;
        private Button? _apply;

        /// <summary>True once the plan was applied (the caller then refreshes the key list and audits the imports).</summary>
        public bool Applied { get; private set; }

        public EnvMigrationDialog(Window? owner, EnvMigrationPlan plan)
            : base(owner, "Import from old .env files", 760, 640, resizable: true)
        {
            _plan = plan;
            _error = FundUi.Text("", 11.5, FundPalette.Kill);
            _error.Visibility = Visibility.Collapsed;
            Closed += (_, _) => _plan.Forget();
            ShowPlan();
        }

        // ------------------------------------------------------------------ phase 1: plan

        private void ShowPlan()
        {
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text("Import from old .env files", 14, FundPalette.TextBright, bold: true));
            var intro = FundUi.Text(
                "UFOS.ai no longer reads .env files: keys live only in the Windows Credential Manager, settings in each module's "
                + "settings file. This one-time import moves what it finds and then removes the .env files. Only NAMES are shown "
                + "here — never values. Every key is checked by reading it back before its line is removed; a line that could not "
                + "be imported stays in its file. No copy of a .env file is kept.", 11.5, FundPalette.Dim);
            intro.Margin = new Thickness(0, 6, 0, 12);
            intro.LineHeight = 17;
            stack.Children.Add(intro);

            if (_plan.Problem != null || _plan.Files.Count == 0)
            {
                stack.Children.Add(FundUi.Text(_plan.Problem ?? "No .env files found — nothing to import.", 12, FundPalette.Text));
                stack.Children.Add(Buttons(applyEnabled: false));
                SetBody(new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = stack });
                return;
            }

            // files
            stack.Children.Add(Section("Files found"));
            foreach (var f in _plan.Files)
            {
                var line = f.DisplayPath + " — " + EnvMigration.ModuleName(f.Module)
                           + (f.ReadError != null ? " — " + f.ReadError + " (left unchanged)" : " — " + f.Entries.Count + " name" + (f.Entries.Count == 1 ? "" : "s"));
                stack.Children.Add(Bullet(line, f.ReadError != null ? FundPalette.Kill : FundPalette.Text));
            }

            // keys
            if (_plan.Keys.Count > 0)
            {
                stack.Children.Add(Section("Keys → Windows Credential Manager"));
                foreach (var kp in _plan.Keys) stack.Children.Add(KeyRow(kp));
            }

            // settings
            if (_plan.Settings.Count > 0)
            {
                stack.Children.Add(Section("Settings → the module's settings file"));
                foreach (var sp in _plan.Settings)
                {
                    var text = EnvMigration.ModuleName(sp.Spec.Module) + ": " + sp.Spec.EnvName + " → " + sp.Spec.JsonKey + " — "
                               + (sp.Problem ?? (sp.IsDefault ? "same as the default, not copied (line removed)" : "will be moved"));
                    stack.Children.Add(Bullet(text, sp.Problem != null ? FundPalette.Risk : FundPalette.Text));
                }
            }

            // process switches
            var switches = _plan.Files.SelectMany(f => f.Entries.Where(e => e.Kind == EnvEntryKind.ProcessSwitch).Select(e => e.Name + " (" + f.DisplayPath + ")")).ToList();
            if (switches.Count > 0)
            {
                stack.Children.Add(Section("Host / port switches — removed"));
                stack.Children.Add(Bullet("No longer used: every backend listens on 127.0.0.1 with its fixed port (remote access was removed with Docker). Removed: "
                                          + string.Join(", ", switches), FundPalette.Dim));
            }

            // unknown names
            var unknown = _plan.Files.SelectMany(f => f.Entries.Where(e => e.Kind == EnvEntryKind.Unknown).Select(e => (File: f, Entry: e))).ToList();
            if (unknown.Count > 0)
            {
                stack.Children.Add(Section("Unknown names — removed unless you keep them"));
                stack.Children.Add(Bullet("Nothing in UFOS.ai uses these. They are never copied anywhere; tick one to leave its line in the file.", FundPalette.Dim));
                foreach (var (file, entry) in unknown)
                {
                    var box = new CheckBox
                    {
                        Content = entry.Name + "  (" + file.DisplayPath + ")",
                        IsChecked = false,
                        Foreground = FundPalette.Text,
                        FontSize = 11.5,
                        Margin = new Thickness(14, 2, 0, 2),
                    };
                    AutomationProperties.SetName(box, "Keep " + entry.Name + " in " + file.DisplayPath);
                    var e = entry;
                    box.Checked += (_, _) => e.KeepInFile = true;
                    box.Unchecked += (_, _) => e.KeepInFile = false;
                    stack.Children.Add(box);
                }
            }

            var note = FundUi.Text(
                "After the import: a .env file with nothing left is deleted; otherwise it is rewritten with only the lines that stay. "
                + "Restart the module backends afterwards (Global Monitoring and the LiveStream Agent read their keys from the Credential Manager from now on).",
                10.5, FundPalette.Faint);
            note.Margin = new Thickness(0, 14, 0, 0);
            stack.Children.Add(note);
            _error.Margin = new Thickness(0, 10, 0, 0);
            stack.Children.Add(_error);
            stack.Children.Add(Buttons(applyEnabled: true));
            SetBody(new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = stack });
        }

        private UIElement KeyRow(KeyPlan kp)
        {
            var panel = new StackPanel { Margin = new Thickness(0, 4, 0, 6) };
            var envNames = string.Join(", ", kp.Sources.Select(s => s.Entry.Name).Distinct());
            var files = string.Join(", ", kp.Sources.Select(s => s.File.DisplayPath).Distinct());
            panel.Children.Add(FundUi.Text(kp.Secret.Label + "  ←  " + envNames + "  (" + files + ")", 12, FundPalette.TextBright));

            string? status = kp.State switch
            {
                KeyPlanState.New => "not stored yet — will be imported",
                KeyPlanState.SameAsStored => kp.Problem ?? "already stored with the same value — the line will be removed",
                KeyPlanState.Broken => kp.Problem + " — not imported",
                KeyPlanState.StoreError => kp.Problem + " — not imported",
                KeyPlanState.DiffersFromStored => "already stored with a DIFFERENT value:",
                KeyPlanState.StoredUnreadable => "stored in an unreadable format (e.g. written by cmdkey):",
                KeyPlanState.SeveralValues => "the .env files contain different values" + (kp.StoredPresent ? " (a key is also stored)" : "") + " — choose one:",
                _ => null,
            };
            var brush = kp.State is KeyPlanState.Broken or KeyPlanState.StoreError ? FundPalette.Kill
                      : kp.NeedsChoice ? FundPalette.Risk : FundPalette.Dim;
            var st = FundUi.Text(status, 11, brush);
            st.Margin = new Thickness(14, 2, 0, 0);
            panel.Children.Add(st);

            if (kp.NeedsChoice)
            {
                var group = "key-" + kp.Secret.Name;
                if (kp.StoredPresent && kp.State != KeyPlanState.StoredUnreadable)
                    panel.Children.Add(Radio(group, "Keep the stored key (discard the .env value)", kp.Choice == -1, () => kp.Choice = -1));
                for (var i = 0; i < kp.ValueGroups.Count; i++)
                {
                    var g = kp.ValueGroups[i];
                    var where = string.Join(", ", g.Select(s => s.File.DisplayPath + " (" + s.Entry.Name + ")").Distinct());
                    var index = i;
                    panel.Children.Add(Radio(group, (kp.StoredPresent ? "Replace it with the value from " : "Use the value from ") + where,
                                             kp.Choice == index, () => kp.Choice = index));
                }
            }
            return panel;
        }

        private static RadioButton Radio(string group, string text, bool isChecked, Action onChecked)
        {
            var rb = new RadioButton
            {
                GroupName = group,
                Content = text,
                IsChecked = isChecked,
                Foreground = FundPalette.Text,
                FontSize = 11.5,
                Margin = new Thickness(28, 3, 0, 0),
            };
            AutomationProperties.SetName(rb, text);
            rb.Checked += (_, _) => onChecked();
            return rb;
        }

        private UIElement Buttons(bool applyEnabled)
        {
            var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 14, 0, 0) };
            var cancel = FundUi.MakeButton(applyEnabled ? "Cancel" : "Close");
            cancel.IsCancel = true;
            cancel.Click += (_, _) => Close();
            buttons.Children.Add(cancel);
            if (applyEnabled)
            {
                _apply = FundUi.MakeButton("Import, verify and remove the .env files", FundPalette.Make(0x267C8CF8), FundPalette.Focus, FundPalette.TextBright);
                _apply.Margin = new Thickness(8, 0, 0, 0);
                AutomationProperties.SetName(_apply, "Import the keys and settings, verify them and remove the .env files");
                _apply.Click += (_, _) => ApplyClicked();
                buttons.Children.Add(_apply);
            }
            return buttons;
        }

        private void ApplyClicked()
        {
            var missing = _plan.Keys.Where(k => k.NeedsChoice && k.Choice == null).Select(k => k.Secret.Label).ToList();
            if (missing.Count > 0)
            {
                _error.Text = "Choose what to do with: " + string.Join(", ", missing) + ".";
                _error.Visibility = Visibility.Visible;
                return;
            }
            if (_apply != null) { _apply.IsEnabled = false; _apply.Content = "Importing…"; }
            try
            {
                EnvMigration.Apply(_plan);
                Applied = true;
            }
            catch (Exception ex)
            {
                // Apply handles every item itself; this is a programming error — say so, never a value.
                UFOS.ai.Logging.AppLog.Warn("HedgeFund", "Import from old .env files failed unexpectedly.", ex);
                _error.Text = "The import stopped unexpectedly (" + ex.GetType().Name + "). Press ↻ Refresh in Keys & data to see which keys are set; "
                              + "the .env files that were not processed are unchanged.";
                _error.Visibility = Visibility.Visible;
                Applied = true;   // something may have been written: the caller refreshes
                return;
            }
            ShowResult();
        }

        // ------------------------------------------------------------------ phase 2: result

        private void ShowResult()
        {
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            var problems = _plan.Keys.Count(k => k.Failed) + _plan.Settings.Count(s => s.Failed) + (_plan.AnyFileProblem ? 1 : 0);
            stack.Children.Add(FundUi.Text(problems == 0 ? "Import finished" : "Import finished with problems", 14,
                                           problems == 0 ? FundPalette.Long : FundPalette.Risk, bold: true));

            if (_plan.Keys.Count > 0)
            {
                stack.Children.Add(Section("Keys"));
                foreach (var kp in _plan.Keys)
                    stack.Children.Add(Bullet(kp.Secret.Label + ": " + kp.Outcome, kp.Failed ? FundPalette.Kill : FundPalette.Text));
            }
            if (_plan.Settings.Count > 0)
            {
                stack.Children.Add(Section("Settings"));
                foreach (var sp in _plan.Settings)
                    stack.Children.Add(Bullet(EnvMigration.ModuleName(sp.Spec.Module) + ": " + sp.Spec.EnvName + " — " + sp.Outcome,
                                              sp.Failed ? FundPalette.Risk : FundPalette.Text));
            }
            stack.Children.Add(Section("Files"));
            foreach (var line in _plan.FileOutcomes) stack.Children.Add(Bullet(line, FundPalette.Text));
            foreach (var f in _plan.Files.Where(f => f.ReadError != null))
                stack.Children.Add(Bullet(f.DisplayPath + ": " + f.ReadError + " — left unchanged", FundPalette.Kill));

            var next = FundUi.Text("Restart the module backends so they read the keys from the Credential Manager. "
                                   + "The LiveStream Agent's YouTube URL can be changed in its window from now on.", 10.5, FundPalette.Faint);
            next.Margin = new Thickness(0, 14, 0, 0);
            stack.Children.Add(next);
            stack.Children.Add(Buttons(applyEnabled: false));
            SetBody(new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = stack });
        }

        // ------------------------------------------------------------------ helpers

        private static TextBlock Section(string title)
        {
            var t = FundUi.Text(title, 12.5, FundPalette.TextBright, bold: true);
            t.Margin = new Thickness(0, 12, 0, 4);
            return t;
        }

        private static TextBlock Bullet(string text, Brush brush)
        {
            var t = FundUi.Text("• " + text, 11.5, brush);
            t.Margin = new Thickness(6, 1, 0, 1);
            return t;
        }
    }
}
