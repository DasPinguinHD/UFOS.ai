using System;
using System.Text.Json;

namespace UFOS.ai.Shared
{
    /// <summary>
    /// Single source of truth for how AI-generated content is labelled in the UI
    /// (EU AI Act Art. 50 — see the "AI content labelling" section of the project's
    /// conventions doc and CLAUDE.md). Every place that shows LLM output uses these
    /// strings so the wording stays identical across modules.
    ///
    /// This file is compiled into the main app AND linked into each module project
    /// (see the &lt;Compile Include="..\Shared\*.cs" Link=...&gt; entries in the module
    /// .csproj files), so a module still works when started on its own. The class is
    /// internal on purpose: each assembly gets its own private copy, so the main app
    /// referencing the module assemblies never sees two public types with the same
    /// name.
    /// </summary>
    internal static class AiContent
    {
        /// <summary>Glyph that marks anything AI-related (trigger buttons, badges).</summary>
        public const string Glyph = "✦";

        /// <summary>Tooltip for every button that starts an AI analysis.</summary>
        public const string TriggerTooltip =
            "Generates an AI analysis via a third-party language model (OpenRouter). " +
            "May contain errors — not investment advice.";

        /// <summary>Caveat line shown directly under every AI output.</summary>
        public const string Caveat =
            "Automated AI analysis — may be inaccurate or incomplete. Not investment advice. Verify independently.";

        /// <summary>Badge text while the model is still working (first exposure is labelled too).</summary>
        public const string LoadingBadge = Glyph + " AI IS ANALYSING…";

        /// <summary>Badge shown when an AI request failed (so an error is never mistaken for a result).</summary>
        public const string UnavailableBadge = Glyph + " AI ANALYSIS UNAVAILABLE";

        /// <summary>
        /// "✦ AI-GENERATED · deepseek-chat-v3-0324 · 18:42" — the visible label that
        /// precedes every AI text. Model and time are optional.
        /// </summary>
        public static string Badge(string? model, DateTimeOffset? generatedAt)
        {
            var text = Glyph + " AI-GENERATED";
            var shortModel = ShortModelName(model);
            if (!string.IsNullOrWhiteSpace(shortModel)) text += " · " + shortModel;
            if (generatedAt.HasValue) text += " · " + generatedAt.Value.ToLocalTime().ToString("HH:mm");
            return text;
        }

        /// <summary>Screen-reader text for an AI output element (always starts with "AI-generated:").</summary>
        public static string AutomationName(string? model, DateTimeOffset? generatedAt)
        {
            var text = "AI-generated content";
            var shortModel = ShortModelName(model);
            if (!string.IsNullOrWhiteSpace(shortModel)) text += ", model " + shortModel;
            if (generatedAt.HasValue) text += ", generated at " + generatedAt.Value.ToLocalTime().ToString("HH:mm");
            return text + ". " + Caveat;
        }

        /// <summary>"deepseek/deepseek-chat-v3-0324" -> "deepseek-chat-v3-0324".</summary>
        public static string ShortModelName(string? model)
        {
            if (string.IsNullOrWhiteSpace(model)) return string.Empty;
            var trimmed = model.Trim();
            var slash = trimmed.LastIndexOf('/');
            return slash >= 0 && slash < trimmed.Length - 1 ? trimmed[(slash + 1)..] : trimmed;
        }

        /// <summary>
        /// Reads the machine-readable "provenance" object the Python backends attach
        /// to every AI response ({"ai_generated": true, "model": ..., "generated_at": ...}).
        /// Accepts either the provenance object itself or a parent that contains it.
        /// </summary>
        public static bool TryReadProvenance(JsonElement element, out string? model, out DateTimeOffset? generatedAt)
        {
            model = null;
            generatedAt = null;
            if (element.ValueKind != JsonValueKind.Object) return false;

            var prov = element;
            if (element.TryGetProperty("provenance", out var nested) && nested.ValueKind == JsonValueKind.Object)
            {
                prov = nested;
            }

            if (!prov.TryGetProperty("ai_generated", out var flag) || flag.ValueKind != JsonValueKind.True)
            {
                return false;
            }

            if (prov.TryGetProperty("model", out var m) && m.ValueKind == JsonValueKind.String)
            {
                model = m.GetString();
            }

            if (prov.TryGetProperty("generated_at", out var g) && g.ValueKind == JsonValueKind.String
                && DateTimeOffset.TryParse(g.GetString(), out var parsed))
            {
                generatedAt = parsed;
            }

            return true;
        }
    }
}
