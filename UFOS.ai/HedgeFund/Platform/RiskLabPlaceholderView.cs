using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using UFOS.ai.HedgeFund.Ui;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// Stage 11 §3: the rail's "Risk Lab" group — an honest placeholder until the Risk Lab view is built (next step of stage 11).
    /// No content is simulated here; the account risk figures live in the Overview's Strategies card.
    /// </summary>
    internal sealed class RiskLabPlaceholderView : UserControl
    {
        public RiskLabPlaceholderView()
        {
            var stack = new StackPanel();
            stack.Children.Add(new SectionHeader("Risk Lab"));
            stack.Children.Add(HfUi.Text("Risk Lab view — next step of stage 11", HfTokens.BodySize, HfTokens.Text, wrap: true));
            var note = HfUi.Text("Until then, the risk figures of the active paper account (VaR, ES, exposure, maximum drawdown, 20-day volatility) are shown in the Overview's Strategies card.",
                                 12, HfTokens.TextDim, wrap: true);
            note.Margin = new Thickness(0, 8, 0, 0);
            stack.Children.Add(note);
            var card = new Card(stack) { VerticalAlignment = VerticalAlignment.Top, MaxWidth = 720, HorizontalAlignment = HorizontalAlignment.Left };
            Content = card;
            AutomationProperties.SetName(this, "Risk Lab: placeholder, the view follows in the next step of stage 11");
        }
    }
}
