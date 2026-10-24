using System;
using System.Collections.Generic;
using System.Linq;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>One input of <see cref="KeyEntryDialog"/>. Secret = PasswordBox (masked), else a TextBox.</summary>
    internal sealed record KeyField(string Label, bool Secret);

    /// <summary>
    /// "Set…" dialog of the Keys section: one masked input per value (the Alpaca key ID and
    /// secret are entered together). Same chrome as the other Hedge Fund popups
    /// (FundPopupWindow: borderless, full-screen safe, drag-restore). The values are only
    /// handed back through <see cref="TakeValues"/>, which clears the inputs; the validator
    /// gets the values to check length/format and returns a message WITHOUT echoing them.
    /// </summary>
    internal sealed class KeyEntryDialog : FundPopupWindow
    {
        private readonly List<Control> _inputs = new();
        private readonly Func<string[], string?> _validate;
        private readonly TextBlock _error;
        private string[]? _accepted;

        public KeyEntryDialog(Window? owner, string title, string message, IReadOnlyList<KeyField> fields,
                              Func<string[], string?> validate, string? footnote)
            : base(owner, title, 480, 220 + 58 * fields.Count, resizable: false)
        {
            _validate = validate;
            SizeToContent = SizeToContent.Height;

            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text(title, 14, FundPalette.TextBright, bold: true));
            var msg = FundUi.Text(message, 12, FundPalette.Dim);
            msg.Margin = new Thickness(0, 8, 0, 12);
            msg.LineHeight = 18;
            stack.Children.Add(msg);

            foreach (var field in fields)
            {
                var label = FundUi.Text(field.Label, 10.5, FundPalette.Faint);
                label.Margin = new Thickness(0, 0, 0, 4);
                stack.Children.Add(label);

                Control input;
                if (field.Secret)
                {
                    input = new PasswordBox
                    {
                        FontSize = 12,
                        FontFamily = FundUi.Mono,
                        Foreground = FundPalette.Text,
                        Background = FundPalette.WindowBg,
                        BorderBrush = FundPalette.Hairline,
                        BorderThickness = new Thickness(1),
                        Padding = new Thickness(8, 5, 8, 5),
                        CaretBrush = FundPalette.Text,
                        MaxLength = CredentialStore.MaxBlobBytes,
                    };
                }
                else
                {
                    var box = FundUi.Input("");
                    box.MaxLength = CredentialStore.MaxBlobBytes;
                    input = box;
                }
                input.Margin = new Thickness(0, 0, 0, 10);
                AutomationProperties.SetName(input, field.Label);
                _inputs.Add(input);
                stack.Children.Add(input);
            }

            _error = FundUi.Text("", 11, FundPalette.Kill);
            _error.Margin = new Thickness(0, 0, 0, 10);
            _error.Visibility = Visibility.Collapsed;
            stack.Children.Add(_error);

            if (!string.IsNullOrWhiteSpace(footnote))
            {
                var note = FundUi.Text(footnote, 10.5, FundPalette.Faint);
                note.FontStyle = FontStyles.Italic;
                note.Margin = new Thickness(0, 0, 0, 12);
                stack.Children.Add(note);
            }

            var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
            var cancel = FundUi.MakeButton("Cancel");
            cancel.IsCancel = true;
            cancel.Click += (_, _) => { ClearInputs(); DialogResult = false; };
            var ok = FundUi.MakeButton("Save to Credential Manager", FundPalette.Make(0x267C8CF8), FundPalette.Focus, FundPalette.TextBright);
            ok.IsDefault = true;
            ok.Margin = new Thickness(8, 0, 0, 0);
            ok.Click += Ok_Click;
            buttons.Children.Add(cancel);
            buttons.Children.Add(ok);
            stack.Children.Add(buttons);
            SetBody(stack);

            Loaded += (_, _) => { if (_inputs.Count > 0) _inputs[0].Focus(); };
            Closed += (_, _) => ClearInputs();
        }

        private void Ok_Click(object sender, RoutedEventArgs e)
        {
            var values = _inputs.Select(ReadInput).ToArray();
            var problem = _validate(values);
            if (problem != null)
            {
                Array.Clear(values);
                _error.Text = problem;
                _error.Visibility = Visibility.Visible;
                return;
            }
            _accepted = values;
            ClearInputs();
            DialogResult = true;
        }

        /// <summary>The accepted values (once); the dialog keeps no reference afterwards.</summary>
        public string[]? TakeValues()
        {
            var values = _accepted;
            _accepted = null;
            return values;
        }

        private static string ReadInput(Control input) => input switch
        {
            PasswordBox p => p.Password ?? "",
            TextBox t => t.Text ?? "",
            _ => "",
        };

        private void ClearInputs()
        {
            foreach (var input in _inputs)
            {
                if (input is PasswordBox p) p.Clear();
                else if (input is TextBox t) t.Clear();
            }
        }
    }
}
