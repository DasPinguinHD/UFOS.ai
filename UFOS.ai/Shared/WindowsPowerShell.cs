using System;
using System.IO;

namespace UFOS.ai.Shared
{
    /// <summary>
    /// Every powershell.exe launch of the app (module backends' run_backend.ps1, the Python package upgrade) goes through
    /// <see cref="ExePath"/>: Windows PowerShell by its absolute path under %SystemRoot%\System32, never the bare name
    /// "powershell.exe" — a bare name is resolved through the working directory and PATH, so a planted powershell.exe
    /// there would run instead (security audit S10-A2, L4).
    ///
    /// The launched files are always .ps1 scripts (-File). <see cref="ScriptArguments"/> keeps -ExecutionPolicy Bypass
    /// for them because it is required: the default policy of Windows client editions is Restricted (no scripts at all),
    /// and under RemoteSigned an unsigned repo script that carries the Mark of the Web (repo downloaded as a ZIP) is
    /// refused too. The switch is process-scoped only (it changes no machine or user setting) and a Group Policy
    /// execution policy still wins over it. -NoProfile: no user/machine profile script runs inside the child.
    ///
    /// Linked into every module project (..\Shared\*.cs), see AiContent.cs.
    /// </summary>
    internal static class WindowsPowerShell
    {
        /// <summary>%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe</summary>
        public static string ExePath => Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.Windows),
            "System32", "WindowsPowerShell", "v1.0", "powershell.exe");

        /// <summary>
        /// Arguments that run the .ps1 at <paramref name="scriptPath"/> with <paramref name="scriptArguments"/>
        /// ("-NoProfile -ExecutionPolicy Bypass [extra switches] -File "&lt;script&gt;" &lt;script arguments&gt;").
        /// </summary>
        public static string ScriptArguments(string scriptPath, string scriptArguments, string? hostSwitches = null)
        {
            if (!scriptPath.EndsWith(".ps1", StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("Only .ps1 scripts are launched through Windows PowerShell: " + scriptPath, nameof(scriptPath));
            var host = string.IsNullOrWhiteSpace(hostSwitches) ? "" : hostSwitches.Trim() + " ";
            var tail = string.IsNullOrWhiteSpace(scriptArguments) ? "" : " " + scriptArguments.Trim();
            return $"-NoProfile -ExecutionPolicy Bypass {host}-File \"{scriptPath}\"{tail}";
        }
    }
}
