using System;
using System.Configuration;
using System.Data;
using System.IO;
using System.Text;
using System.Windows;

namespace UFOS.ai
{
    /// <summary>
    /// Interaction logic for App.xaml
    /// </summary>
    public partial class App : Application
    {
        // Public helper used by windows to display/log UI errors
        public void ShowUiException(Exception ex)
        {
            try
            {
                // log to backend data logs folder
                try
                {
                    var logsDir = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "..", "..", "backend", "data", "logs");
                    logsDir = Path.GetFullPath(logsDir);
                    if (!Directory.Exists(logsDir)) Directory.CreateDirectory(logsDir);
                    var path = Path.Combine(logsDir, "ui-errors.log");
                    var txt = $"[{DateTime.Now:O}] {ex?.ToString() ?? "(null)"}\n";
                    File.AppendAllText(path, txt, Encoding.UTF8);
                }
                catch { }

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
