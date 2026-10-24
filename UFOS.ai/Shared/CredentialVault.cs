using System;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;

namespace UFOS.ai.Shared
{
    /// <summary>What is stored under a target. Unreadable = something is stored, but not as UTF-8 text without NUL bytes
    /// (e.g. UTF-16 written by cmdkey / the Credential Manager UI) — the readers can't use it; set it again.</summary>
    internal enum VaultState { NotSet, Set, Unreadable }

    /// <summary>A Credential Manager failure. The message is user-facing and NEVER contains a value.</summary>
    internal sealed class CredentialVaultException : Exception
    {
        public int Win32Error { get; }

        public CredentialVaultException(string message, int win32Error = 0) : base(message)
        {
            Win32Error = win32Error;
        }
    }

    /// <summary>
    /// The one Windows Credential Manager access layer of every WPF module (S10-E, user decision 2026-10-24: no .env, keys
    /// only in the Credential Manager). Linked into every module project (..\Shared\*.cs).
    ///
    /// Format (must match the Python readers, HedgeFund/backend/strategy_platform/secrets.py and its shared copies):
    /// generic credential (CRED_TYPE_GENERIC), TargetName = the full target ("UFOS.ai/&lt;scope&gt;/&lt;name&gt;", see
    /// CredentialCatalog), CredentialBlob = UTF-8 bytes of the trimmed value, Persist = CRED_PERSIST_LOCAL_MACHINE (this
    /// PC, this Windows user, DPAPI-encrypted by Windows). UserName is informational only.
    ///
    /// Security: values are never logged and never put into exception messages. <see cref="ReadValue"/> exists only for
    /// code that must SEND a key itself (the main app's OpenRouter call); everything else uses GetState / ValueEquals.
    /// Byte buffers that held a value are zeroed after use (managed strings can't be wiped — residual risk, as is that any
    /// program running under the same Windows user can read these credentials).
    /// </summary>
    internal static class CredentialVault
    {
        /// <summary>CRED_MAX_CREDENTIAL_BLOB_SIZE (5 * 512 bytes).</summary>
        public const int MaxBlobBytes = 2560;

        /// <summary>Stores the trimmed value under <paramref name="target"/> (creates or replaces the credential).</summary>
        /// <exception cref="ArgumentException">Empty value.</exception>
        /// <exception cref="CredentialVaultException">Too long, or the Credential Manager refused it.</exception>
        public static void Write(string target, string label, string comment, string value)
        {
            var trimmed = (value ?? "").Trim();
            if (trimmed.Length == 0) throw new ArgumentException("The value is empty.", nameof(value));

            var bytes = Encoding.UTF8.GetBytes(trimmed);
            var buffer = IntPtr.Zero;
            try
            {
                if (bytes.Length > MaxBlobBytes)
                {
                    throw new CredentialVaultException(
                        $"The value is too long ({bytes.Length} bytes) — the Windows Credential Manager allows at most {MaxBlobBytes} bytes.");
                }

                buffer = Marshal.AllocHGlobal(bytes.Length);
                Marshal.Copy(bytes, 0, buffer, bytes.Length);

                var credential = new CREDENTIAL
                {
                    Flags = 0,
                    Type = CRED_TYPE_GENERIC,
                    TargetName = target,
                    Comment = comment,
                    CredentialBlobSize = (uint)bytes.Length,
                    CredentialBlob = buffer,
                    Persist = CRED_PERSIST_LOCAL_MACHINE,
                    AttributeCount = 0,
                    Attributes = IntPtr.Zero,
                    TargetAlias = null,
                    UserName = Environment.UserName,
                };
                if (!CredWrite(ref credential, 0))
                {
                    var error = Marshal.GetLastWin32Error();
                    throw new CredentialVaultException(
                        $"The Windows Credential Manager refused to store \"{label}\" (error {error}).", error);
                }
            }
            finally
            {
                Array.Clear(bytes);
                if (buffer != IntPtr.Zero)
                {
                    Marshal.Copy(new byte[bytes.Length], 0, buffer, bytes.Length);
                    Marshal.FreeHGlobal(buffer);
                }
            }
        }

        /// <summary>Deletes the credential. False when it did not exist.</summary>
        public static bool Delete(string target, string label)
        {
            if (CredDelete(target, CRED_TYPE_GENERIC, 0)) return true;
            var error = Marshal.GetLastWin32Error();
            if (error == ERROR_NOT_FOUND) return false;
            throw new CredentialVaultException(
                $"The Windows Credential Manager could not remove \"{label}\" (error {error}).", error);
        }

        /// <summary>
        /// NotSet (missing or blank — the stripped value must not be empty, same rule as secrets.py), Set (valid UTF-8 without
        /// NUL bytes) or Unreadable. Throws CredentialVaultException when the store can't be read.
        /// </summary>
        public static VaultState GetState(string target) => WithBlob(target, Classify);

        /// <summary>Length in UTF-8 bytes of the stored, trimmed value (0 when not set) — for the import's read-back check.</summary>
        public static int StoredLength(string target) => WithBlob(target, blob =>
        {
            if (Classify(blob) != VaultState.Set) return 0;
            var trimmed = TrimmedUtf8(blob!);
            try { return trimmed.Length; }
            finally { Array.Clear(trimmed); }
        });

        /// <summary>True when the stored value equals the trimmed candidate (byte-wise UTF-8, fixed time). The stored value
        /// never leaves this class.</summary>
        public static bool ValueEquals(string target, string candidate)
        {
            var wanted = Encoding.UTF8.GetBytes((candidate ?? "").Trim());
            try
            {
                return WithBlob(target, blob =>
                {
                    if (blob == null || Classify(blob) != VaultState.Set) return false;
                    var stored = TrimmedUtf8(blob);
                    try { return System.Security.Cryptography.CryptographicOperations.FixedTimeEquals(stored, wanted); }
                    finally { Array.Clear(stored); }
                });
            }
            finally
            {
                Array.Clear(wanted);
            }
        }

        /// <summary>
        /// The trimmed value, or null when nothing (non-blank) is stored. ONLY for code that must send the key itself;
        /// never log, display or persist the result. Throws CredentialVaultException when the store can't be read or the
        /// stored value is unreadable (the message names <paramref name="label"/>, never the value).
        /// </summary>
        public static string? ReadValue(string target, string label) => WithBlob(target, blob =>
        {
            var state = Classify(blob);
            if (state == VaultState.NotSet) return null;
            if (state == VaultState.Unreadable)
                throw new CredentialVaultException(
                    $"\"{label}\" is stored in the Windows Credential Manager in a format UFOS.ai can't read — set it again in ⚙ Keys & data.");
            var trimmed = TrimmedUtf8(blob!);
            try { return Encoding.UTF8.GetString(trimmed); }
            finally { Array.Clear(trimmed); }
        });

        // ---------- helpers ----------

        /// <summary>Reads the blob into a temporary array, runs <paramref name="use"/>, zeroes the array.</summary>
        private static T WithBlob<T>(string target, Func<byte[]?, T> use)
        {
            if (!CredRead(target, CRED_TYPE_GENERIC, 0, out var pointer))
            {
                var error = Marshal.GetLastWin32Error();
                if (error == ERROR_NOT_FOUND) return use(null);
                throw new CredentialVaultException($"The Windows Credential Manager could not be read (error {error}).", error);
            }

            byte[]? blob = null;
            try
            {
                var credential = Marshal.PtrToStructure<CREDENTIAL>(pointer);
                var size = (int)credential.CredentialBlobSize;
                blob = new byte[size];
                if (size > 0 && credential.CredentialBlob != IntPtr.Zero)
                {
                    Marshal.Copy(credential.CredentialBlob, blob, 0, size);
                }
                return use(blob);
            }
            finally
            {
                if (blob != null) Array.Clear(blob);
                CredFree(pointer);
            }
        }

        /// <summary>Classifies a blob without keeping it (the decoded chars are wiped).</summary>
        private static VaultState Classify(byte[]? blob)
        {
            if (blob == null || blob.Length == 0) return VaultState.NotSet;
            if (Array.IndexOf(blob, (byte)0) >= 0) return VaultState.Unreadable; // NUL bytes: e.g. UTF-16LE
            char[] chars;
            try { chars = new UTF8Encoding(false, true).GetChars(blob); }
            catch (ArgumentException) { return VaultState.Unreadable; }       // not valid UTF-8
            try { return chars.All(char.IsWhiteSpace) ? VaultState.NotSet : VaultState.Set; }
            finally { Array.Clear(chars); }
        }

        /// <summary>The blob decoded as UTF-8, trimmed, re-encoded (all temporaries wiped).</summary>
        private static byte[] TrimmedUtf8(byte[] blob)
        {
            char[] chars = Encoding.UTF8.GetChars(blob);
            try
            {
                int start = 0, end = chars.Length - 1;
                while (start <= end && char.IsWhiteSpace(chars[start])) start++;
                while (end >= start && char.IsWhiteSpace(chars[end])) end--;
                return Encoding.UTF8.GetBytes(chars, start, end - start + 1);
            }
            finally
            {
                Array.Clear(chars);
            }
        }

        // ---------- advapi32 (wincred.h) ----------

        private const uint CRED_TYPE_GENERIC = 1;
        private const uint CRED_PERSIST_LOCAL_MACHINE = 2;
        private const int ERROR_NOT_FOUND = 1168;

        // CREDENTIALW. String fields marshal as LPWSTR (CharSet.Unicode); the blob is a raw pointer to our own
        // zeroed-after-use buffer. Sequential layout = natural alignment, matching the native struct on x86 and x64.
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct CREDENTIAL
        {
            public uint Flags;
            public uint Type;
            public string? TargetName;
            public string? Comment;
            public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
            public uint CredentialBlobSize;
            public IntPtr CredentialBlob;
            public uint Persist;
            public uint AttributeCount;
            public IntPtr Attributes;
            public string? TargetAlias;
            public string? UserName;
        }

        [DllImport("advapi32.dll", EntryPoint = "CredWriteW", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CredWrite([In] ref CREDENTIAL credential, uint flags);

        [DllImport("advapi32.dll", EntryPoint = "CredReadW", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CredRead(string targetName, uint type, uint reservedFlag, out IntPtr credential);

        [DllImport("advapi32.dll", EntryPoint = "CredDeleteW", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CredDelete(string targetName, uint type, uint flags);

        [DllImport("advapi32.dll", EntryPoint = "CredFree", SetLastError = false)]
        private static extern void CredFree(IntPtr buffer);
    }
}
