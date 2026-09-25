using System.Windows;
using UFOS.ai.Shared;

namespace UFOS.ai.GlobalMonitoring
{
    public partial class App : Application
    {
        // Only runs when this module is started on its own (GlobalMonitoring.exe).
        // Inside the main UFOS.ai app the notice was already shown by the main App.
        protected override void OnStartup(StartupEventArgs e)
        {
            base.OnStartup(e);
            AiDisclaimerWindow.ConfirmAtStartup(this);
        }
    }
}
