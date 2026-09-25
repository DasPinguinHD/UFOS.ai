using System;
using System.Collections.Generic;
using System.IO;

namespace UFOS.ai.Services
{
    // Minimal .env reader for the main WPF app, mirroring the same per-module .env
    // convention LiveStreamAgent/HedgeFund's Python backends already use (key=value,
    // gitignored, .env.example documents the shape) - just extended to C#. Walks up
    // from the build output to find UFOS.ai/.env next to UFOS.ai.csproj.
    public static class EnvConfig
    {
        private static readonly Dictionary<string, string> Values = Load();

        public static string? Get(string key)
        {
            var fromEnvironment = Environment.GetEnvironmentVariable(key);
            if (!string.IsNullOrEmpty(fromEnvironment))
            {
                return fromEnvironment;
            }

            return Values.TryGetValue(key, out var value) ? value : null;
        }

        private static Dictionary<string, string> Load()
        {
            var result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            try
            {
                var dir = new DirectoryInfo(AppDomain.CurrentDomain.BaseDirectory);
                for (int i = 0; i < 8 && dir is not null; i++, dir = dir.Parent)
                {
                    var csprojPath = Path.Combine(dir.FullName, "UFOS.ai.csproj");
                    var envPath = Path.Combine(dir.FullName, ".env");
                    if (!File.Exists(csprojPath) || !File.Exists(envPath))
                    {
                        continue;
                    }

                    foreach (var line in File.ReadAllLines(envPath))
                    {
                        var trimmed = line.Trim();
                        if (trimmed.Length == 0 || trimmed.StartsWith('#'))
                        {
                            continue;
                        }

                        var separatorIndex = trimmed.IndexOf('=');
                        if (separatorIndex <= 0)
                        {
                            continue;
                        }

                        var key = trimmed[..separatorIndex].Trim();
                        var value = trimmed[(separatorIndex + 1)..].Trim().Trim('"');
                        result[key] = value;
                    }

                    break;
                }
            }
            catch
            {
                // no .env present/readable - callers treat missing keys as "not configured"
            }

            return result;
        }
    }
}
