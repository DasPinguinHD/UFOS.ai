using System;
using System.Collections.Generic;
using System.Linq;
using UFOS.ai.Shared;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>One credential of the solution (S10-E: every module's keys, from Shared/CredentialCatalog.cs). EnvNames = the
    /// legacy .env names, used ONLY by the one-time .env import; Target = the full Credential Manager target.</summary>
    internal sealed record SecretInfo(string Name, string Label, IReadOnlyList<string> EnvNames, bool IsSecretValue, string Target, string UsedBy)
    {
        public static SecretInfo From(CredentialEntry e) => new(e.Name, e.Label, e.LegacyEnvNames, e.IsSecretValue, e.Target, e.UsedBy);
    }

    /// <summary>What is stored for a key. Unreadable = something is stored, but not as the UTF-8 text the
    /// backend expects (e.g. UTF-16 written by cmdkey / the Credential Manager UI, NUL bytes) — set it again.</summary>
    internal enum KeyState { NotSet, Set, Unreadable }

    /// <summary>A Credential Manager failure. The message is user-facing and NEVER contains a value.</summary>
    internal sealed class CredentialStoreException : Exception
    {
        public int Win32Error { get; }

        public CredentialStoreException(string message, int win32Error = 0) : base(message)
        {
            Win32Error = win32Error;
        }
    }

    /// <summary>
    /// Writes, removes and checks the keys of ALL modules in the Windows Credential Manager (Strategy Platform stage 1,
    /// contract: backend/strategy_platform/SPEC.md §2; S10-E: every module, list in Shared/CredentialCatalog.cs, the access
    /// layer in Shared/CredentialVault.cs — no module reads a .env any more).
    ///
    /// Format (must match secrets.py, which only READS): generic credential (CRED_TYPE_GENERIC),
    /// TargetName = the catalog target "UFOS.ai/&lt;Module&gt;/&lt;name&gt;", CredentialBlob = UTF-8 bytes of the trimmed
    /// value, Persist = CRED_PERSIST_LOCAL_MACHINE (stays on this PC, for this Windows user;
    /// DPAPI-encrypted by Windows). UserName is informational only (Environment.UserName).
    ///
    /// Security: values are never logged, never put into exception messages and never
    /// returned to callers — there is deliberately no "get value" method. Byte buffers that
    /// held a value are zeroed after use (managed strings can't be wiped; residual risk, as
    /// is that any program running under the same Windows user can read the credentials).
    /// </summary>
    internal static class CredentialStore
    {
        public const string TargetPrefix = "UFOS.ai/HedgeFund/";

        /// <summary>CRED_MAX_CREDENTIAL_BLOB_SIZE (5 * 512 bytes).</summary>
        public const int MaxBlobBytes = CredentialVault.MaxBlobBytes;

        // S10-E: the list of ALL modules' credentials lives in Shared/CredentialCatalog.cs (Hedge Fund names unchanged, matching
        // backend/strategy_platform/secrets.py; Global Monitoring / LiveStream Agent names matching their backend/credstore.py).
        public static readonly IReadOnlyList<SecretInfo> Secrets = CredentialCatalog.All.Select(SecretInfo.From).ToArray();

        public const string AlpacaKeyId = "alpaca-paper-key-id";
        public const string AlpacaSecretKey = "alpaca-paper-secret-key";
        public const string BinanceDemoApiKey = "binance-demo-api-key";
        public const string BinanceDemoApiSecret = "binance-demo-api-secret";

        /// <summary>
        /// Stage 10 (S10-K): keys of RETIRED integrations (Kraken Futures demo, retired 2026-10-19). Not used by anything; Keys &amp; data
        /// lists such a key ONLY while it is still stored in the Credential Manager, with "Remove" as the only action.
        /// </summary>
        public static readonly IReadOnlyList<SecretInfo> RetiredSecrets = CredentialCatalog.Retired.Select(SecretInfo.From).ToArray();

        public static SecretInfo? Find(string name) => Secrets.FirstOrDefault(s => s.Name == name);

        /// <summary>The full Credential Manager target of a (current or retired) key.</summary>
        public static string Target(string name) => Require(name).Target;

        // ---------- public API (S10-E: the Credential Manager access itself lives in Shared/CredentialVault.cs) ----------

        /// <summary>Stores the trimmed value (creates or replaces the credential).</summary>
        /// <exception cref="ArgumentException">Unknown name or empty value.</exception>
        /// <exception cref="CredentialStoreException">Too long, or the Credential Manager refused it.</exception>
        public static void Set(string name, string value)
        {
            var info = Require(name);
            Vault(() => { CredentialVault.Write(info.Target, info.Label, "UFOS.ai — " + info.Label, value); return true; });
        }

        /// <summary>Deletes the credential. False when it did not exist.</summary>
        public static bool Remove(string name)
        {
            var info = Require(name);
            return Vault(() => CredentialVault.Delete(info.Target, info.Label));
        }

        /// <summary>
        /// True when something non-blank is stored (Set OR Unreadable — the import asks before
        /// replacing either). Throws CredentialStoreException when the store can't be read.
        /// </summary>
        public static bool IsSet(string name) => GetState(name) != KeyState.NotSet;

        /// <summary>
        /// NotSet (missing or blank, same rule as secrets.py: the stripped value must not be empty),
        /// Set (valid UTF-8 without NUL bytes) or Unreadable (invalid UTF-8 or NUL bytes, e.g. a
        /// UTF-16LE blob written by cmdkey / the Credential Manager UI — the backend can't use it).
        /// Throws CredentialStoreException when the store can't be read.
        /// </summary>
        public static KeyState GetState(string name)
        {
            var info = Require(name);
            return Vault(() => CredentialVault.GetState(info.Target)) switch
            {
                VaultState.Set => KeyState.Set,
                VaultState.Unreadable => KeyState.Unreadable,
                _ => KeyState.NotSet,
            };
        }

        /// <summary>
        /// True when the stored value equals the trimmed candidate (byte-wise, UTF-8). Used by the
        /// .env import to decide whether to ask. The stored value never leaves the vault.
        /// </summary>
        public static bool ValueEquals(string name, string candidate)
        {
            var info = Require(name);
            return Vault(() => CredentialVault.ValueEquals(info.Target, candidate));
        }

        // ---------- helpers ----------

        // Retired keys too (S10-K): they are listed while stored so they can be checked and removed.
        private static SecretInfo Require(string name)
            => Find(name) ?? RetiredSecrets.FirstOrDefault(s => s.Name == name)
               ?? throw new ArgumentException("Unknown key name.", nameof(name));

        /// <summary>Runs a vault call; its failures keep surfacing as CredentialStoreException (same messages, never a value).</summary>
        private static T Vault<T>(Func<T> call)
        {
            try { return call(); }
            catch (CredentialVaultException ex) { throw new CredentialStoreException(ex.Message, ex.Win32Error); }
        }
    }
}
