using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Net.Http;
using System.Threading.Tasks;
using System.Windows.Media;
using System.Windows.Media.Imaging;

namespace UFOS.ai.Services
{
    // LLM vendor behind an OpenRouter model id ("anthropic/claude-..." -> Anthropic).
    // Domain is null for vendors we don't know - those only get the initials circle.
    internal sealed record VendorInfo(string Name, string? Domain, string Initials);

    // Vendor name + logo for the Transaction Log.
    //
    // The logos are the vendors' own favicons, fetched at runtime (Google's public
    // favicon service) purely to identify which provider served a request - no logo
    // artwork is drawn or bundled with UFOS.ai. Each favicon is cached in memory per
    // domain and on disk under %LOCALAPPDATA%\UFOS.ai\logo-cache\<domain>.png so the
    // list still shows them offline later. Until a favicon is loaded (or when it can't
    // be) the UI shows a neutral grey circle with the vendor's initials.
    internal static class VendorLogos
    {
        private static readonly HttpClient HttpClient = new() { Timeout = TimeSpan.FromSeconds(10) };

        // model-id prefix -> vendor
        private static readonly Dictionary<string, VendorInfo> KnownPrefixes = new(StringComparer.OrdinalIgnoreCase)
        {
            ["anthropic"] = new("Anthropic", "anthropic.com", "A"),
            ["openai"] = new("OpenAI", "openai.com", "OA"),
            ["google"] = new("Google", "google.com", "G"),
            ["deepseek"] = new("DeepSeek", "deepseek.com", "DS"),
            ["meta-llama"] = new("Meta", "meta.com", "M"),
            ["mistralai"] = new("Mistral AI", "mistral.ai", "MA"),
            ["x-ai"] = new("xAI", "x.ai", "xA"),
            ["qwen"] = new("Qwen", "qwenlm.github.io", "Q"),
            ["cohere"] = new("Cohere", "cohere.com", "C"),
            ["perplexity"] = new("Perplexity", "perplexity.ai", "P"),
            ["amazon"] = new("Amazon", "aws.amazon.com", "Am"),
            ["microsoft"] = new("Microsoft", "microsoft.com", "MS"),
            ["nvidia"] = new("NVIDIA", "nvidia.com", "NV"),
        };

        // Bare model names without a "vendor/" prefix (e.g. an older ledger line).
        private static readonly (string NameStart, string Prefix)[] BareNameHints =
        {
            ("claude", "anthropic"), ("gpt", "openai"), ("o1", "openai"), ("o3", "openai"), ("o4", "openai"),
            ("gemini", "google"), ("gemma", "google"), ("deepseek", "deepseek"), ("llama", "meta-llama"),
            ("mistral", "mistralai"), ("mixtral", "mistralai"), ("grok", "x-ai"), ("qwen", "qwen"),
            ("command", "cohere"), ("sonar", "perplexity"), ("nova", "amazon"), ("phi", "microsoft"),
            ("nemotron", "nvidia"),
        };

        private static readonly ConcurrentDictionary<string, ImageSource> MemoryCache = new(StringComparer.OrdinalIgnoreCase);
        private static readonly ConcurrentDictionary<string, Task<ImageSource?>> Pending = new(StringComparer.OrdinalIgnoreCase);
        private static readonly ConcurrentDictionary<string, DateTime> FailedAtUtc = new(StringComparer.OrdinalIgnoreCase);
        private static readonly TimeSpan RetryAfterFailure = TimeSpan.FromMinutes(5);

        public static string CacheDirectory => Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "UFOS.ai", "logo-cache");

        public static VendorInfo Resolve(string? modelId)
        {
            var id = (modelId ?? string.Empty).Trim().TrimStart('~');
            if (id.Length == 0)
            {
                return new VendorInfo("Unknown vendor", null, "?");
            }

            var slash = id.IndexOf('/');
            if (slash > 0)
            {
                var prefix = id[..slash];
                if (KnownPrefixes.TryGetValue(prefix, out var known))
                {
                    return known;
                }
                var name = char.ToUpperInvariant(prefix[0]) + prefix[1..];
                var initials = prefix.Length >= 2
                    ? char.ToUpperInvariant(prefix[0]).ToString() + prefix[1]
                    : char.ToUpperInvariant(prefix[0]).ToString();
                return new VendorInfo(name, null, initials);
            }

            foreach (var (nameStart, hintPrefix) in BareNameHints)
            {
                if (id.StartsWith(nameStart, StringComparison.OrdinalIgnoreCase) &&
                    KnownPrefixes.TryGetValue(hintPrefix, out var hinted))
                {
                    return hinted;
                }
            }
            return new VendorInfo("Unknown vendor", null, "?");
        }

        // Model id without the "vendor/" prefix: "anthropic/claude-opus-5.5" -> "claude-opus-5.5".
        public static string ShortModelName(string? modelId)
        {
            var id = (modelId ?? string.Empty).Trim();
            var slash = id.IndexOf('/');
            return slash > 0 && slash < id.Length - 1 ? id[(slash + 1)..] : (id.Length == 0 ? "unknown model" : id);
        }

        public static bool TryGetCached(string domain, out ImageSource? logo)
        {
            if (MemoryCache.TryGetValue(domain, out var cached))
            {
                logo = cached;
                return true;
            }
            logo = null;
            return false;
        }

        // Frozen image (usable from any thread) or null when unavailable. Never throws.
        public static Task<ImageSource?> GetLogoAsync(string domain)
        {
            if (MemoryCache.TryGetValue(domain, out var cached))
            {
                return Task.FromResult<ImageSource?>(cached);
            }
            if (FailedAtUtc.TryGetValue(domain, out var failedAt) && DateTime.UtcNow - failedAt < RetryAfterFailure)
            {
                return Task.FromResult<ImageSource?>(null);
            }
            var task = Pending.GetOrAdd(domain, d => LoadAsync(d));
            if (task.IsCompleted)
            {
                // LoadAsync finished synchronously, before GetOrAdd stored it - don't keep it.
                Pending.TryRemove(new KeyValuePair<string, Task<ImageSource?>>(domain, task));
            }
            return task;
        }

        private static async Task<ImageSource?> LoadAsync(string domain)
        {
            try
            {
                var cacheFile = Path.Combine(CacheDirectory, domain + ".png");

                // 1) Disk cache (works offline).
                try
                {
                    if (File.Exists(cacheFile))
                    {
                        var diskBytes = await File.ReadAllBytesAsync(cacheFile).ConfigureAwait(false);
                        var fromDisk = Decode(diskBytes);
                        if (fromDisk is not null)
                        {
                            MemoryCache[domain] = fromDisk;
                            return fromDisk;
                        }
                    }
                }
                catch (IOException)
                {
                    // fall through to the network
                }
                catch (UnauthorizedAccessException)
                {
                }

                // 2) The vendor's own favicon via Google's favicon service.
                var url = "https://www.google.com/s2/favicons?domain=" + Uri.EscapeDataString(domain) + "&sz=64";
                var bytes = await HttpClient.GetByteArrayAsync(url).ConfigureAwait(false);
                var image = Decode(bytes);
                if (image is null)
                {
                    FailedAtUtc[domain] = DateTime.UtcNow;
                    return null;
                }

                MemoryCache[domain] = image;
                FailedAtUtc.TryRemove(domain, out _);
                try
                {
                    Directory.CreateDirectory(CacheDirectory);
                    await File.WriteAllBytesAsync(cacheFile, bytes).ConfigureAwait(false);
                }
                catch
                {
                    // Disk cache is best effort.
                }
                return image;
            }
            catch
            {
                FailedAtUtc[domain] = DateTime.UtcNow;
                return null;
            }
            finally
            {
                Pending.TryRemove(domain, out _);
            }
        }

        private static ImageSource? Decode(byte[] bytes)
        {
            if (bytes.Length == 0)
            {
                return null;
            }
            try
            {
                using var stream = new MemoryStream(bytes);
                var bitmap = new BitmapImage();
                bitmap.BeginInit();
                bitmap.CacheOption = BitmapCacheOption.OnLoad;
                bitmap.DecodePixelWidth = 64;
                bitmap.StreamSource = stream;
                bitmap.EndInit();
                bitmap.Freeze();
                return bitmap;
            }
            catch
            {
                return null;
            }
        }
    }
}
