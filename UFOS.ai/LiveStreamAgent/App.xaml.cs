using System;
using System.Configuration;
using System.Data;
using System.IO;
using System.Text;
using System.Windows;
using UFOS.ai.Logging;
using UFOS.ai.Shared;

namespace UFOS.ai.LiveStreamAgent
{
    /// <summary>
    /// Interaction logic for App.xaml
    /// </summary>
    public partial class App : Application, IUiErrorReporter
    {
        // Only runs when this module is started on its own (LiveStreamAgent.exe).
        // Inside the main UFOS.ai app the notice was already shown by the main App.
        protected override void OnStartup(StartupEventArgs e)
        {
            base.OnStartup(e);
            AiDisclaimerWindow.ConfirmAtStartup(this);
        }

        // Public helper used by windows to display/log UI errors. Routes through the
        // central AppLog so the error shows up in the global log window regardless of
        // which module raised it.
        public void ShowUiException(Exception ex)
        {
            try
            {
                try { AppLog.Error("UI", ex?.Message ?? "(unbekannter Fehler)", ex); } catch { }

                // show minimal user-facing notification
                try
                {
                    var msgBox = new Window
                    {
                        Title = "Error",
                        Content = "Something went wrong: " + ex?.Message ?? "No error message provided",
                        SizeToContent = SizeToContent.WidthAndHeight,
                        WindowStartupLocation = WindowStartupLocation.CenterScreen,
                        ResizeMode = ResizeMode.NoResize,
                        Topmost = true
                    };

                    msgBox.Show();

                }
                catch { }
            }
            catch { }
        }
    }

}
