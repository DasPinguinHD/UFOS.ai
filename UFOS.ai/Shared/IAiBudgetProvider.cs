using System.Collections.Generic;

namespace UFOS.ai.Shared
{
    /// <summary>
    /// One module backend that owns AI budgets (task B, contract UFOS.ai/docs/ai-budgets-spec.md §5/§6).
    /// The MainWindow "AI budgets" window (AiBudgetsWindow) lists every registered provider; the
    /// backend stays the authority for its limits and spend (compartmentalization) — the app only
    /// reads <c>GET /ai/budgets</c> and writes ONE budget per <c>PUT /ai/budgets</c>.
    ///
    /// How a new module plugs in (contract §5):
    ///  1. Backend: copy <c>ai_caps.py</c> (unchanged) into the backend, declare its <c>CapDef</c>s,
    ///     call <c>reserve()</c>/<c>settle()</c> around every LLM call (BEFORE the call, fail closed),
    ///     add <c>GET/PUT /ai/budgets</c> behind its <c>api_auth</c> token (PUT refused when an
    ///     <c>Origin</c> header is present), persist via the module's own DB. Only the JSON of §4 is
    ///     binding (schema 1, <c>module</c> = <see cref="ModuleId"/>).
    ///  2. App: add ONE entry to <see cref="AiBudgetProviders.All"/> (Shared/AiBudgetProviders.cs).
    ///     Nothing else — no window change; the window renders every registered provider in order.
    ///     A static list (compile time) was chosen over scanning module.json files: no file parsing,
    ///     nothing on disk can add a provider.
    ///  3. Cost log: the module's cost_ledger writes lines with its <c>module</c> name; list that name
    ///     in <see cref="CostLogModules"/> and the features in each budget's <c>cost_log</c> pairs.
    ///
    /// Linked into the main app AND every module csproj (..\Shared\*.cs) — internal, no main-app types.
    /// </summary>
    internal interface IAiBudgetProvider
    {
        /// <summary>"hedgefund" — must equal the payload's "module".</summary>
        string ModuleId { get; }

        /// <summary>"Hedge Fund" — card header in the window.</summary>
        string Label { get; }

        /// <summary>{ "HedgeFund", "backend" } — resolved with <see cref="BackendAuth.FindBackendDir"/>
        /// (folder containing main.py; its data/api-token holds the per-start token).</summary>
        string[] BackendRelativeDir { get; }

        /// <summary>8867 — always http://127.0.0.1:&lt;Port&gt; (never built from payload data).</summary>
        int Port { get; }

        /// <summary>{ "HedgeFund" } — cost-log "module" names of this backend (informational totals).</summary>
        IReadOnlyList<string> CostLogModules { get; }

        /// <summary>"Open the Hedge Fund module to start its backend."</summary>
        string StartHint { get; }
    }

    /// <summary>
    /// App-local variant (contract §5 step 2b): a provider WITHOUT a backend (e.g. the main app's
    /// "Summarize with AI", <see cref="AiBudgetAppCaps"/>) implements this as well; AiBudgetRegistry then
    /// answers LoadAsync/PutAsync in-process with the same schema-1 payload / PUT result instead of HTTP
    /// (Port 0, no backend folder). It must enforce its caps itself BEFORE each call (fail closed) and
    /// persist them in a small JSON under %LOCALAPPDATA%\UFOS.ai\ (never .env).
    /// </summary>
    internal interface IAiBudgetLocalSource : IAiBudgetProvider
    {
        /// <summary>The schema-1 module payload (module = ModuleId). Must not throw for a corrupt store —
        /// report it as a status "error" entry.</summary>
        AiBudgetPayload LocalPayload();

        /// <summary>Stores ONE limit (validated here: key, min..max) → Success(payload with Changed) or Failure.</summary>
        AiBudgetPutResult LocalPut(string key, decimal limitUsd);
    }
}
