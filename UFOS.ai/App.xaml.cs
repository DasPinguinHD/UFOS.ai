using System;
using System.Configuration;
using System.Data;
using System.IO;
using System.Text;
using System.Windows;
using UFOS.ai.Logging;
using UFOS.ai.Shared;

namespace UFOS.ai
{
    /// <summary>
    /// Interaction logic for App.xaml
    /// </summary>
    public partial class App : Application, IUiErrorReporter
    {
        // EU AI Act / "not investment advice" notice: shown on every start, before
        // MainWindow (StartupUri) is created. Declining exits the app. See
        // Shared/AiDisclaimerWindow.cs and the AI-labelling section in CLAUDE.md.
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
                    MessageBox.Show("An Error has occured. Details have been logged. Try restarting the App. Please report any bugs on the GitHub.", "Error", MessageBoxButton.OK, MessageBoxImage.Error);
                }
                catch { }
            }
            catch { }
        }
    }

}
