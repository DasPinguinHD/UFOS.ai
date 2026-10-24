using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;
using UFOS.ai.HedgeFund.Newsroom;

namespace UFOS.ai.HedgeFund.Platform
{
    // =====================================================================================
    // Strategy Platform stage 9 — Shadow Account (contract: backend/strategy_platform/SPEC.md §19.6–§19.8, routes §19.10,
    // exact JSON §19.11, backend notes §19.12, WPF notes §19.13). Your broker statements are parsed by the LOCAL backend
    // (loopback, session token) and stored in hedgefund.db — nothing is uploaded anywhere; only a user-triggered AI
    // commentary sends NUMBERS (no names, ISINs, symbols or file names). The CSV upload is multipart/form-data with ONE
    // part "file" (the backend reads it in chunks and enforces the limits). Every text that comes from an imported file is
    // shown as plain text by the views (never evaluated).
    // =====================================================================================

    internal static partial class PlatformClient
    {
        private static readonly TimeSpan ShadowTimeout = TimeSpan.FromSeconds(20);
        /// <summary>Import (≤ 5 MB / 50,000 rows), diagnostics, shadows and the report read prices from the data layer.</summary>
        private static readonly TimeSpan ShadowHeavyTimeout = TimeSpan.FromSeconds(120);

        public const string ShadowTemplateAuto = "auto";
        public const string ShadowTemplateGeneric = "generic";

        public static Task<PlatformResult<ShTemplates>> GetShadowTemplatesAsync()
            => SendAsync<ShTemplates>(HttpMethod.Get, "/v2/shadow/templates", null, ShadowTimeout);

        public static Task<PlatformResult<ShProfileList>> GetShadowProfilesAsync()
            => SendAsync<ShProfileList>(HttpMethod.Get, "/v2/shadow/profiles", null, ShadowTimeout);

        /// <summary>POST /v2/shadow/profiles {name: 1..60, mapping} → 201 {profile} (a new version per name); 400 per mapping field.</summary>
        public static Task<PlatformResult<ShProfileEnvelope>> CreateShadowProfileAsync(string name, Dictionary<string, object?> mapping)
            => SendAsync<ShProfileEnvelope>(HttpMethod.Post, "/v2/shadow/profiles",
                new Dictionary<string, object?> { ["name"] = name.Trim(), ["mapping"] = mapping }, ShadowTimeout);

        /// <summary>§19.15: GET /v2/shadow/fx?year= → stored FRED DEXUSEU coverage of that year (Shadow conversions only — not tax data).</summary>
        public static Task<PlatformResult<ShFx>> GetShadowFxAsync(int year)
            => SendAsync<ShFx>(HttpMethod.Get, "/v2/shadow/fx?year=" + year.ToString(CultureInfo.InvariantCulture), null, ShadowTimeout);

        /// <summary>§19.15: POST /v2/shadow/fx {year} — loads FRED DEXUSEU (network; FRED key from the Credential Manager; 503 fx_unavailable, 409 future_year).</summary>
        public static Task<PlatformResult<ShFx>> RefreshShadowFxAsync(int year)
            => SendAsync<ShFx>(HttpMethod.Post, "/v2/shadow/fx", new Dictionary<string, object?> { ["year"] = year }, TimeSpan.FromSeconds(180));

        public static Task<PlatformResult<ShAccountList>> GetShadowAccountsAsync()
            => SendAsync<ShAccountList>(HttpMethod.Get, "/v2/shadow/accounts", null, ShadowTimeout);

        /// <summary>POST /v2/shadow/accounts {label: 1..60, base_currency: EUR|USD, broker_country: DE|other} → 201 {account}.</summary>
        public static Task<PlatformResult<ShAccountEnvelope>> CreateShadowAccountAsync(string label, string baseCurrency, string brokerCountry)
            => SendAsync<ShAccountEnvelope>(HttpMethod.Post, "/v2/shadow/accounts", new Dictionary<string, object?>
            {
                ["label"] = label.Trim(),
                ["base_currency"] = baseCurrency,
                ["broker_country"] = brokerCountry,
            }, ShadowTimeout);

        /// <summary>GET /v2/shadow/accounts/{id} → {account, imports, positions, unmatched, valuation}.</summary>
        public static Task<PlatformResult<ShAccountDetail>> GetShadowAccountAsync(string accountId)
            => SendAsync<ShAccountDetail>(HttpMethod.Get, "/v2/shadow/accounts/" + Esc(accountId), null, ShadowHeavyTimeout);

        /// <summary>
        /// §19.14: POST /v2/shadow/accounts/{id}/imports/{import_id}/remove {reason?} (fix1023: no confirm phrase, note optional) →
        /// {removed: {import_id, transactions, ts}, account} — deletes that batch's database rows only (never a file), audited; 404.
        /// </summary>
        public static Task<PlatformResult<JsonElement>> RemoveShadowImportAsync(string accountId, string importId, string? reason)
            => SendAsync<JsonElement>(HttpMethod.Post, "/v2/shadow/accounts/" + Esc(accountId) + "/imports/" + Esc(importId) + "/remove",
                WithNote(new Dictionary<string, object?>(), reason), ShadowTimeout);   // fix1023: no confirm phrase

        public static Task<PlatformResult<ShTxnList>> GetShadowTransactionsAsync(string accountId, int limit, int offset)
            => SendAsync<ShTxnList>(HttpMethod.Get, "/v2/shadow/accounts/" + Esc(accountId) + "/transactions?limit="
                + Math.Clamp(limit, 1, 1000).ToString(CultureInfo.InvariantCulture) + "&offset=" + Math.Max(0, offset).ToString(CultureInfo.InvariantCulture),
                null, ShadowTimeout);

        /// <summary>
        /// PUT /v2/shadow/accounts/{id}/valuation {as_of, prices: [{key, price, currency?}] (1..500)} → {valuation}. §20.7: a price with a
        /// currency values only that (instrument, currency) position — the dialog sends the position's own currency.
        /// </summary>
        public static Task<PlatformResult<JsonElement>> PutShadowValuationAsync(string accountId, string asOf, IReadOnlyList<(string Key, double Price, string? Currency)> prices)
            => SendAsync<JsonElement>(HttpMethod.Put, "/v2/shadow/accounts/" + Esc(accountId) + "/valuation", new Dictionary<string, object?>
            {
                ["as_of"] = asOf,
                ["prices"] = prices.Select(p =>
                {
                    var d = new Dictionary<string, object?> { ["key"] = p.Key, ["price"] = p.Price };
                    if (!string.IsNullOrWhiteSpace(p.Currency)) d["currency"] = p.Currency;
                    return d;
                }).ToList(),
            }, ShadowTimeout);

        public static Task<PlatformResult<ShClassificationList>> GetShadowClassificationsAsync()
            => SendAsync<ShClassificationList>(HttpMethod.Get, "/v2/shadow/classifications", null, ShadowTimeout);

        /// <summary>PUT /v2/shadow/classifications {key, asset_class, reason?} → {classification}.</summary>
        public static Task<PlatformResult<JsonElement>> PutShadowClassificationAsync(string key, string assetClass, string? reason)
        {
            var body = new Dictionary<string, object?> { ["key"] = key, ["asset_class"] = assetClass };
            if (!string.IsNullOrWhiteSpace(reason)) body["reason"] = reason.Trim();
            return SendAsync<JsonElement>(HttpMethod.Put, "/v2/shadow/classifications", body, ShadowTimeout);
        }

        // ----- §20.10 / §20.11 symbol mapping + price sources (global; insert-only, audited by the backend) -----

        /// <summary>GET /v2/shadow/price-sources → the configured source per data type + the EODHD daily budget.</summary>
        public static Task<PlatformResult<ShPriceSources>> GetShadowPriceSourcesAsync()
            => SendAsync<ShPriceSources>(HttpMethod.Get, "/v2/shadow/price-sources", null, ShadowTimeout);

        /// <summary>GET /v2/shadow/listings?isin= → EODHD search candidates of that ISIN (1 EODHD call, 0 when cached).</summary>
        public static Task<PlatformResult<ShListings>> GetShadowListingsAsync(string isin)
            => SendAsync<ShListings>(HttpMethod.Get, "/v2/shadow/listings?isin=" + Uri.EscapeDataString(isin), null, ShadowHeavyTimeout);

        /// <summary>
        /// GET /v2/shadow/symbol-check?ticker=&amp;isin= (fix1023 round 2) — read-only check of a US ticker against the cached Alpaca
        /// asset list (Alpaca's asset name, usable / refused_reason, an existing mapping). 503 symbol_check_unavailable, 400 bad input.
        /// </summary>
        public static Task<PlatformResult<ShSymbolCheck>> CheckShadowSymbolAsync(string ticker, string? isin)
            => SendAsync<ShSymbolCheck>(HttpMethod.Get, "/v2/shadow/symbol-check?ticker=" + Uri.EscapeDataString(ticker)
                                                       + (string.IsNullOrWhiteSpace(isin) ? "" : "&isin=" + Uri.EscapeDataString(isin!)), null, ShadowTimeout);

        public static Task<PlatformResult<ShSymbolMappings>> GetShadowSymbolMappingsAsync()
            => SendAsync<ShSymbolMappings>(HttpMethod.Get, "/v2/shadow/symbol-mappings", null, ShadowTimeout);

        /// <summary>PUT /v2/shadow/symbol-mappings {isin, symbol, reason?} — a US ticker, checked against Alpaca (fix1023: no confirm flag).</summary>
        public static Task<PlatformResult<ShSymbolMappingEnvelope>> PutShadowUsTickerMappingAsync(string isin, string symbol, string? reason)
        {
            var body = new Dictionary<string, object?> { ["isin"] = isin, ["symbol"] = symbol };   // fix1023: confirm_us_listing no longer required
            if (!string.IsNullOrWhiteSpace(reason)) body["reason"] = reason.Trim();
            return SendAsync<ShSymbolMappingEnvelope>(HttpMethod.Put, "/v2/shadow/symbol-mappings", body, ShadowHeavyTimeout);
        }

        /// <summary>PUT /v2/shadow/symbol-mappings {isin, source: "eodhd", listing, reason?} — one cached search candidate (fix1023: no confirm flag).</summary>
        public static Task<PlatformResult<ShSymbolMappingEnvelope>> PutShadowListingMappingAsync(string isin, string listing, string? reason)
        {
            var body = new Dictionary<string, object?> { ["isin"] = isin, ["source"] = "eodhd", ["listing"] = listing };   // fix1023: confirm_listing no longer required
            if (!string.IsNullOrWhiteSpace(reason)) body["reason"] = reason.Trim();
            return SendAsync<ShSymbolMappingEnvelope>(HttpMethod.Put, "/v2/shadow/symbol-mappings", body, ShadowTimeout);
        }

        /// <summary>§20.11 review fixes: POST /v2/shadow/price-sources/eodhd/refresh {listing} — fetches again now, costs 1 EODHD call
        /// (audited). A failed refresh is 200 with ok false + reason; the limit gives 409 eodhd_limit; not one of your listings → 404.</summary>
        public static Task<PlatformResult<ShEodhdRefresh>> RefreshShadowEodhdListingAsync(string listing)
            => SendAsync<ShEodhdRefresh>(HttpMethod.Post, "/v2/shadow/price-sources/eodhd/refresh",
                new Dictionary<string, object?> { ["listing"] = listing }, ShadowHeavyTimeout);

        /// <summary>DELETE /v2/shadow/symbol-mappings/{isin} → {removed: {isin, symbol, ts}}; 404 not_found.</summary>
        public static Task<PlatformResult<JsonElement>> DeleteShadowSymbolMappingAsync(string isin)
            => SendAsync<JsonElement>(HttpMethod.Delete, "/v2/shadow/symbol-mappings/" + Esc(isin), null, ShadowTimeout);

        /// <summary>GET /v2/shadow/accounts/{id}/diagnostics?benchmark= → behaviour diagnostics (each block may be {unavailable}).</summary>
        public static Task<PlatformResult<JsonElement>> GetShadowDiagnosticsAsync(string accountId, string benchmark)
            => SendAsync<JsonElement>(HttpMethod.Get, "/v2/shadow/accounts/" + Esc(accountId) + "/diagnostics?benchmark=" + Esc(benchmark.Trim().ToUpperInvariant()),
                null, ShadowHeavyTimeout);

        /// <summary>POST /v2/shadow/accounts/{id}/shadows {rules: [1..5]} → user vs rule portfolios on the same cash flows.</summary>
        public static Task<PlatformResult<JsonElement>> RunShadowPortfoliosAsync(string accountId, IReadOnlyList<Dictionary<string, object?>> rules)
            => SendAsync<JsonElement>(HttpMethod.Post, "/v2/shadow/accounts/" + Esc(accountId) + "/shadows",
                new Dictionary<string, object?> { ["rules"] = rules.ToList() }, ShadowHeavyTimeout);

        /// <summary>POST /v2/shadow/accounts/{id}/report {format: html|json, rules?, benchmark?} → {filename, content_type, content, sha256, generated_at}.</summary>
        public static Task<PlatformResult<ShReportFile>> ExportShadowReportAsync(string accountId, string format, IReadOnlyList<Dictionary<string, object?>>? rules, string? benchmark)
        {
            if (format != "html" && format != "json") throw new ArgumentException("Unknown format.", nameof(format));
            var body = new Dictionary<string, object?> { ["format"] = format };
            if (rules != null && rules.Count > 0) body["rules"] = rules.ToList();
            if (!string.IsNullOrWhiteSpace(benchmark)) body["benchmark"] = benchmark.Trim().ToUpperInvariant();
            return SendAsync<ShReportFile>(HttpMethod.Post, "/v2/shadow/accounts/" + Esc(accountId) + "/report", body, ShadowHeavyTimeout);
        }

        /// <summary>POST /v2/shadow/accounts/{id}/commentary {model, language, benchmark?} → {commentary} (budget key "reports"; numbers only).</summary>
        public static Task<PlatformResult<MonCommentaryEnvelope>> CreateShadowCommentaryAsync(string accountId, string model, string language, string? benchmark)
            => SendAsync<MonCommentaryEnvelope>(HttpMethod.Post, "/v2/shadow/accounts/" + Esc(accountId) + "/commentary",
                CommentaryBody(model, language, benchmark), CommentaryTimeout);

        /// <summary>
        /// POST /v2/shadow/accounts/{id}/imports?template=&amp;profile_id=&amp;dry_run= (multipart, ONE part "file") → 200 dry run /
        /// 201 import (ImportResult); 409 unknown_format (+ detection, read with <see cref="ReadShadowRefusal"/>); 400 too_large /
        /// empty / binary / encoding / malformed / too_many_rows / too_many_columns / csv_invalid / profile_required.
        /// profile_id is sent only with template=generic (the backend refuses it otherwise).
        /// </summary>
        public static async Task<PlatformResult<ShImportResult>> ImportShadowFileAsync(string accountId, byte[] data, string fileName,
                                                                                       string template, string? profileId, bool dryRun)
        {
            var q = new List<string> { "template=" + Esc(template), "dry_run=" + (dryRun ? "true" : "false") };
            if (template == ShadowTemplateGeneric && !string.IsNullOrWhiteSpace(profileId)) q.Add("profile_id=" + Esc(profileId));
            var path = "/v2/shadow/accounts/" + Esc(accountId) + "/imports?" + string.Join("&", q);
            try
            {
                using var client = NewsroomBackend.CreateClient(ShadowHeavyTimeout);
                using var request = new HttpRequestMessage(HttpMethod.Post, NewsroomBackend.BaseUrl + path);
                var form = new MultipartFormDataContent();
                var part = new ByteArrayContent(data);
                part.Headers.ContentType = new MediaTypeHeaderValue("text/csv");
                form.Add(part, "file", SafeUploadName(fileName));
                request.Content = form;
                using var resp = await client.SendAsync(request);
                string text;
                try { text = await resp.Content.ReadAsStringAsync(); }
                catch { text = ""; }
                if (!resp.IsSuccessStatusCode) return Fail<ShImportResult>(resp.StatusCode, text);
                try
                {
                    var value = string.IsNullOrWhiteSpace(text) ? null : JsonSerializer.Deserialize<ShImportResult>(text, Json);
                    return new PlatformResult<ShImportResult> { Status = PlatformCallStatus.Ok, Value = value, HttpStatus = (int)resp.StatusCode };
                }
                catch (Exception ex) when (ex is JsonException || ex is InvalidOperationException || ex is NotSupportedException)
                {
                    return new PlatformResult<ShImportResult>
                    {
                        Status = PlatformCallStatus.Error,
                        HttpStatus = (int)resp.StatusCode,
                        Message = "Unexpected response from the backend (/v2/shadow/…/imports) — backend and app versions may not match. Details: " + ex.Message,
                    };
                }
            }
            catch (Exception ex)
            {
                var timedOut = ex is TaskCanceledException || ex is OperationCanceledException;
                return new PlatformResult<ShImportResult>
                {
                    Status = PlatformCallStatus.Offline,
                    Message = timedOut ? "The backend did not answer in time." : BackendNotRunning,
                };
            }
        }

        /// <summary>A refused import's body {error, code, detection?} (409 unknown_format carries the detection), or null.</summary>
        public static ShImportRefusal? ReadShadowRefusal<T>(PlatformResult<T> result)
        {
            if (result.Ok || string.IsNullOrWhiteSpace(result.RawBody)) return null;
            try { return JsonSerializer.Deserialize<ShImportRefusal>(result.RawBody, Json); }
            catch (Exception ex) when (ex is JsonException || ex is NotSupportedException || ex is InvalidOperationException) { return null; }
        }

        /// <summary>A JSON element as a typed DTO (for blocks kept raw), or null.</summary>
        public static T? ReadAs<T>(JsonElement? element) where T : class
        {
            if (element is not JsonElement e || e.ValueKind != JsonValueKind.Object) return null;
            try { return e.Deserialize<T>(Json); }
            catch (Exception ex) when (ex is JsonException || ex is NotSupportedException || ex is InvalidOperationException) { return null; }
        }

        /// <summary>The file name sent in the multipart header: the last path segment, printable ASCII only, ≤ 120 characters.</summary>
        private static string SafeUploadName(string? name)
        {
            var leaf = System.IO.Path.GetFileName(name ?? "") ?? "";
            var clean = new string(leaf.Where(c => c >= 0x20 && c < 0x7F && c != '"' && c != '\\').ToArray()).Trim();
            if (clean.Length == 0) clean = "statement.csv";
            return clean.Length <= 120 ? clean : clean.Substring(clean.Length - 120);
        }
    }

    // =====================================================================================
    // DTOs — exactly §19.11 / §19.12. Blocks whose members may be {unavailable: reason} (diagnostics, shadows) are read
    // from the raw JsonElement by the view (ShJson). No computed DTO property without [JsonIgnore].
    // =====================================================================================

    internal sealed class ShTemplates
    {
        [JsonPropertyName("templates")] public List<ShTemplate>? Templates { get; set; }
        [JsonPropertyName("canonical_fields")] public List<string>? CanonicalFields { get; set; }
        [JsonPropertyName("kinds")] public List<string>? Kinds { get; set; }
        [JsonPropertyName("asset_classes")] public List<string>? AssetClasses { get; set; }
        [JsonPropertyName("limits")] public ShLimits? Limits { get; set; }
        [JsonPropertyName("notice")] public string? Notice { get; set; }
    }

    internal sealed class ShTemplate
    {
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("label")] public string? Label { get; set; }
        [JsonPropertyName("verify")] public bool? Verify { get; set; }
        [JsonPropertyName("verify_note")] public string? VerifyNote { get; set; }
        [JsonPropertyName("required_columns")] public List<string>? RequiredColumns { get; set; }
        [JsonPropertyName("type_map")] public Dictionary<string, string>? TypeMap { get; set; }
        /// <summary>§19.16: what the template never reads / stores (e.g. Trade Republic counterparty data).</summary>
        [JsonPropertyName("privacy")] public string? Privacy { get; set; }
    }

    internal sealed class ShLimits
    {
        [JsonPropertyName("max_bytes")] public long? MaxBytes { get; set; }
        [JsonPropertyName("max_rows")] public int? MaxRows { get; set; }
        [JsonPropertyName("max_columns")] public int? MaxColumns { get; set; }
        [JsonPropertyName("max_field_chars")] public int? MaxFieldChars { get; set; }
    }

    internal sealed class ShProfileList
    {
        [JsonPropertyName("profiles")] public List<ShProfile>? Profiles { get; set; }
    }

    internal sealed class ShProfileEnvelope
    {
        [JsonPropertyName("profile")] public ShProfile? Profile { get; set; }
    }

    internal sealed class ShProfile
    {
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("version")] public int? Version { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("mapping")] public JsonElement? Mapping { get; set; }
    }

    internal sealed class ShAccountList
    {
        [JsonPropertyName("accounts")] public List<ShAccount>? Accounts { get; set; }
        [JsonPropertyName("notice")] public string? Notice { get; set; }
    }

    internal sealed class ShAccountEnvelope
    {
        [JsonPropertyName("account")] public ShAccount? Account { get; set; }
    }

    internal sealed class ShAccount
    {
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("label")] public string? Label { get; set; }
        [JsonPropertyName("base_currency")] public string? BaseCurrency { get; set; }
        [JsonPropertyName("broker_country")] public string? BrokerCountry { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("imports")] public int? Imports { get; set; }
        [JsonPropertyName("transactions")] public int? Transactions { get; set; }
        [JsonPropertyName("first_date")] public string? FirstDate { get; set; }
        [JsonPropertyName("last_date")] public string? LastDate { get; set; }
        [JsonPropertyName("templates")] public List<string>? Templates { get; set; }

        [JsonIgnore] public string DisplayName => (string.IsNullOrWhiteSpace(Label) ? "(no label)" : Label!) + " · " + (Id ?? "?");
    }

    internal sealed class ShAccountDetail
    {
        [JsonPropertyName("account")] public ShAccount? Account { get; set; }
        [JsonPropertyName("imports")] public List<ShImportSummary>? Imports { get; set; }
        [JsonPropertyName("positions")] public List<ShPosition>? Positions { get; set; }
        [JsonPropertyName("unmatched")] public List<ShUnmatched>? Unmatched { get; set; }
        [JsonPropertyName("valuation")] public ShValuation? Valuation { get; set; }
    }

    internal sealed class ShImportSummary
    {
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("account_id")] public string? AccountId { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("filename")] public string? FileName { get; set; }
        [JsonPropertyName("sha256")] public string? Sha256 { get; set; }
        [JsonPropertyName("template")] public string? Template { get; set; }
        [JsonPropertyName("profile_id")] public string? ProfileId { get; set; }
        [JsonPropertyName("rows")] public int? Rows { get; set; }
        [JsonPropertyName("accepted")] public int? Accepted { get; set; }
        [JsonPropertyName("rejected")] public int? Rejected { get; set; }
        [JsonPropertyName("duplicates")] public int? Duplicates { get; set; }
        [JsonPropertyName("skipped")] public int? Skipped { get; set; }
    }

    internal sealed class ShPosition
    {
        [JsonPropertyName("key")] public string? Key { get; set; }
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("isin")] public string? Isin { get; set; }
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("asset_class")] public string? AssetClass { get; set; }
        [JsonPropertyName("currency")] public string? Currency { get; set; }
        [JsonPropertyName("qty")] public double? Qty { get; set; }
        [JsonPropertyName("cost")] public double? Cost { get; set; }
        [JsonPropertyName("avg_cost")] public double? AvgCost { get; set; }
        [JsonPropertyName("lots")] public JsonElement? Lots { get; set; }
    }

    internal sealed class ShUnmatched
    {
        [JsonPropertyName("date")] public string? Date { get; set; }
        [JsonPropertyName("key")] public string? Key { get; set; }
        [JsonPropertyName("qty")] public double? Qty { get; set; }
    }

    internal sealed class ShValuation
    {
        [JsonPropertyName("as_of")] public string? AsOf { get; set; }
        [JsonPropertyName("prices")] public List<ShPrice>? Prices { get; set; }
        [JsonPropertyName("ts")] public string? Ts { get; set; }
    }

    internal sealed class ShPrice
    {
        [JsonPropertyName("key")] public string? Key { get; set; }
        [JsonPropertyName("price")] public double? Price { get; set; }
        /// <summary>§20.7: the currency of the valued position (optional).</summary>
        [JsonPropertyName("currency")] public string? Currency { get; set; }
    }

    internal sealed class ShImportResult
    {
        [JsonPropertyName("import")] public ShImportSummary? Import { get; set; }
        [JsonPropertyName("detection")] public ShDetection? Detection { get; set; }
        [JsonPropertyName("preview")] public List<ShTxn>? Preview { get; set; }
        [JsonPropertyName("counts")] public ShCounts? Counts { get; set; }
        [JsonPropertyName("errors")] public List<ShRowError>? Errors { get; set; }
        [JsonPropertyName("warnings")] public List<string>? Warnings { get; set; }
    }

    internal sealed class ShImportRefusal
    {
        [JsonPropertyName("error")] public string? Error { get; set; }
        [JsonPropertyName("code")] public string? Code { get; set; }
        [JsonPropertyName("detection")] public ShDetection? Detection { get; set; }
    }

    internal sealed class ShDetection
    {
        [JsonPropertyName("template")] public string? Template { get; set; }
        [JsonPropertyName("score")] public JsonElement? Score { get; set; }
        [JsonPropertyName("encoding")] public string? Encoding { get; set; }
        [JsonPropertyName("delimiter")] public string? Delimiter { get; set; }
        [JsonPropertyName("header")] public List<string>? Header { get; set; }
        [JsonPropertyName("missing")] public List<string>? Missing { get; set; }
    }

    internal sealed class ShCounts
    {
        [JsonPropertyName("rows")] public int? Rows { get; set; }
        [JsonPropertyName("accepted")] public int? Accepted { get; set; }
        [JsonPropertyName("rejected")] public int? Rejected { get; set; }
        [JsonPropertyName("duplicates")] public int? Duplicates { get; set; }
        [JsonPropertyName("skipped")] public int? Skipped { get; set; }
        /// <summary>§19.16: rows whose broker transaction id is already imported (skipped, not added again).</summary>
        [JsonPropertyName("already_known")] public int? AlreadyKnown { get; set; }
        /// <summary>§19.16 review fixes (v20): rows whose content matches an earlier import (skipped; reasons in the row list).</summary>
        [JsonPropertyName("possible_duplicates")] public int? PossibleDuplicates { get; set; }
        /// <summary>§20.5: positions found in the file (e.g. an IBKR Flex Open Positions snapshot).</summary>
        [JsonPropertyName("positions")] public int? Positions { get; set; }
    }

    internal sealed class ShRowError
    {
        [JsonPropertyName("row")] public int? Row { get; set; }
        [JsonPropertyName("field")] public string? Field { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
    }

    internal sealed class ShTxnList
    {
        [JsonPropertyName("transactions")] public List<ShTxn>? Transactions { get; set; }
        [JsonPropertyName("total")] public int? Total { get; set; }
    }

    internal sealed class ShTxn
    {
        [JsonPropertyName("id")] public JsonElement? Id { get; set; }
        [JsonPropertyName("date")] public string? Date { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("key")] public string? Key { get; set; }
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("isin")] public string? Isin { get; set; }
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("asset_class")] public string? AssetClass { get; set; }
        [JsonPropertyName("qty")] public double? Qty { get; set; }
        [JsonPropertyName("price")] public double? Price { get; set; }
        [JsonPropertyName("currency")] public string? Currency { get; set; }
        [JsonPropertyName("gross")] public double? Gross { get; set; }
        [JsonPropertyName("fee")] public double? Fee { get; set; }
        [JsonPropertyName("tax")] public double? Tax { get; set; }
        [JsonPropertyName("net")] public double? Net { get; set; }
        [JsonPropertyName("import_id")] public string? ImportId { get; set; }
        [JsonPropertyName("row")] public int? Row { get; set; }
        [JsonPropertyName("raw_type")] public string? RawType { get; set; }
        // §19.16 (storage v19): broker transaction id, the amount / currency / FX rate as reported, a note (e.g. "staking reward")
        [JsonPropertyName("ext_id")] public string? ExtId { get; set; }
        /// <summary>§19.16 review fixes (v20): the broker's timestamp (no transaction detail view exists — not shown).</summary>
        [JsonPropertyName("ts")] public string? Ts { get; set; }
        [JsonPropertyName("original_amount")] public double? OriginalAmount { get; set; }
        [JsonPropertyName("original_currency")] public string? OriginalCurrency { get; set; }
        [JsonPropertyName("fx_rate")] public double? FxRate { get; set; }
        [JsonPropertyName("note")] public string? Note { get; set; }
    }

    /// <summary>Display texts of the normalised transaction kinds (§19.6 + §19.16).</summary>
    internal static class ShText
    {
        public static string Kind(string? kind) => kind switch
        {
            "buy" => "buy",
            "sell" => "sell",
            "dividend" => "dividend",
            "interest" => "interest",
            "withholding" => "withholding tax",
            "fee" => "fee",
            "deposit" => "deposit",
            "withdrawal" => "withdrawal",
            "tax_refund" => "tax refund",
            "staking" => "staking reward",
            "pending_buy" => "pending private-market order",
            "corporate_action" => "corporate action (unpaired)",
            null or "" => "—",
            _ => kind!,
        };

        /// <summary>"12.50 USD @ 1.0850" — the original amount as reported (never recomputed), or "".</summary>
        public static string Original(ShTxn t)
        {
            if (t.OriginalAmount is not double a) return "";
            return UFOS.ai.HedgeFund.Fund.FundFormat.Num(a, 2) + (string.IsNullOrWhiteSpace(t.OriginalCurrency) ? "" : " " + t.OriginalCurrency)
                   + (t.FxRate is double fx ? " @ " + fx.ToString("0.######", System.Globalization.CultureInfo.InvariantCulture) : "");
        }

        /// <summary>Note + broker id, plain text.</summary>
        public static string NoteAndId(ShTxn t)
        {
            var parts = new List<string>();
            if (!string.IsNullOrWhiteSpace(t.Note)) parts.Add(t.Note!);
            if (!string.IsNullOrWhiteSpace(t.ExtId)) parts.Add("id " + t.ExtId);
            return string.Join(" · ", parts);
        }
    }

    internal sealed class ShClassificationList
    {
        [JsonPropertyName("classifications")] public List<ShClassification>? Classifications { get; set; }
    }

    internal sealed class ShClassification
    {
        [JsonPropertyName("key")] public string? Key { get; set; }
        [JsonPropertyName("asset_class")] public string? AssetClass { get; set; }
        [JsonPropertyName("ts")] public string? Ts { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
    }

    /// <summary>§19.15 /v2/shadow/fx.</summary>
    internal sealed class ShFx
    {
        [JsonPropertyName("series")] public string? Series { get; set; }
        [JsonPropertyName("year")] public int? Year { get; set; }
        [JsonPropertyName("observations")] public int? Observations { get; set; }
        [JsonPropertyName("first")] public string? First { get; set; }
        [JsonPropertyName("last")] public string? Last { get; set; }
        [JsonPropertyName("fetched_at")] public string? FetchedAt { get; set; }
        [JsonPropertyName("added")] public int? Added { get; set; }
    }

    internal sealed class ShReportFile
    {
        [JsonPropertyName("filename")] public string? FileName { get; set; }
        [JsonPropertyName("content_type")] public string? ContentType { get; set; }
        [JsonPropertyName("content")] public string? Content { get; set; }
        [JsonPropertyName("sha256")] public string? Sha256 { get; set; }
        [JsonPropertyName("generated_at")] public string? GeneratedAt { get; set; }
    }

    // ----- §20.10 / §20.11 DTOs (exact JSON in SPEC §20.11) -----

    internal sealed class ShEodhdBudget
    {
        [JsonPropertyName("plan")] public string? Plan { get; set; }
        [JsonPropertyName("day")] public string? Day { get; set; }
        [JsonPropertyName("daily_limit")] public int? DailyLimit { get; set; }
        [JsonPropertyName("used_today")] public int? UsedToday { get; set; }
        [JsonPropertyName("left_today")] public int? LeftToday { get; set; }
        [JsonPropertyName("resets")] public string? Resets { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
    }

    internal sealed class ShPriceSource
    {
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("label")] public string? Label { get; set; }
        [JsonPropertyName("currency")] public string? Currency { get; set; }
        [JsonPropertyName("key_name")] public string? KeyName { get; set; }
        [JsonPropertyName("key_present")] public bool? KeyPresent { get; set; }
        [JsonPropertyName("key_error")] public string? KeyError { get; set; }
        [JsonPropertyName("budget")] public ShEodhdBudget? Budget { get; set; }
        [JsonPropertyName("cached_listings_today")] public int? CachedListingsToday { get; set; }
        [JsonPropertyName("terms")] public string? Terms { get; set; }
        [JsonPropertyName("pairs")] public Dictionary<string, string>? Pairs { get; set; }
        /// <summary>§20.11 review fixes: failed EODHD fetches cached for the UTC day (reason verbatim until tomorrow or a refresh).</summary>
        [JsonPropertyName("failures_today")] public List<ShEodhdFailure>? FailuresToday { get; set; }
        [JsonPropertyName("refresh")] public string? Refresh { get; set; }
    }

    internal sealed class ShEodhdFailure
    {
        [JsonPropertyName("listing")] public string? Listing { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("fetched_at")] public string? FetchedAt { get; set; }
    }

    internal sealed class ShEodhdLastClose
    {
        [JsonPropertyName("date")] public string? Date { get; set; }
        [JsonPropertyName("close")] public double? Close { get; set; }
    }

    internal sealed class ShEodhdRefresh
    {
        [JsonPropertyName("listing")] public string? Listing { get; set; }
        [JsonPropertyName("ok")] public bool? Ok { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("closes")] public int? Closes { get; set; }
        [JsonPropertyName("last")] public ShEodhdLastClose? Last { get; set; }
        [JsonPropertyName("cost")] public string? Cost { get; set; }
        [JsonPropertyName("budget")] public ShEodhdBudget? Budget { get; set; }
    }

    internal sealed class ShPriceSources
    {
        [JsonPropertyName("order")] public List<string>? Order { get; set; }
        [JsonPropertyName("sources")] public List<ShPriceSource>? Sources { get; set; }
        [JsonPropertyName("no_source")] public List<string>? NoSource { get; set; }
        [JsonPropertyName("later")] public List<string>? Later { get; set; }
    }

    internal sealed class ShListingCandidate
    {
        [JsonPropertyName("listing")] public string? Listing { get; set; }
        [JsonPropertyName("code")] public string? Code { get; set; }
        [JsonPropertyName("exchange")] public string? Exchange { get; set; }
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("type")] public string? Type { get; set; }
        [JsonPropertyName("country")] public string? Country { get; set; }
        [JsonPropertyName("currency")] public string? Currency { get; set; }
        [JsonPropertyName("is_primary")] public bool? IsPrimary { get; set; }
        [JsonPropertyName("previous_close")] public double? PreviousClose { get; set; }
        [JsonPropertyName("previous_close_date")] public string? PreviousCloseDate { get; set; }
    }

    internal sealed class ShListings
    {
        [JsonPropertyName("isin")] public string? Isin { get; set; }
        [JsonPropertyName("source")] public string? Source { get; set; }
        [JsonPropertyName("cached")] public bool? Cached { get; set; }
        [JsonPropertyName("searched_at")] public string? SearchedAt { get; set; }
        [JsonPropertyName("candidates")] public List<ShListingCandidate>? Candidates { get; set; }
        [JsonPropertyName("budget")] public ShEodhdBudget? Budget { get; set; }
    }

    internal sealed class ShSymbolMapping
    {
        [JsonPropertyName("isin")] public string? Isin { get; set; }
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("exchange")] public string? Exchange { get; set; }
        [JsonPropertyName("asset_name")] public string? AssetName { get; set; }
        [JsonPropertyName("confirmed_us_listing")] public bool? ConfirmedUsListing { get; set; }
        [JsonPropertyName("confirmed_listing")] public bool? ConfirmedListing { get; set; }
        /// <summary>fix1023 round 2: "dialog" (saved without a flag) | "confirm_flag" (older mappings / flag sent).</summary>
        [JsonPropertyName("confirmed_via")] public string? ConfirmedVia { get; set; }
        [JsonPropertyName("source")] public string? Source { get; set; }
        [JsonPropertyName("price_source")] public string? PriceSource { get; set; }
        [JsonPropertyName("listing")] public string? Listing { get; set; }
        [JsonPropertyName("currency")] public string? Currency { get; set; }
        [JsonPropertyName("validated_against")] public string? ValidatedAgainst { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
    }

    internal sealed class ShSymbolMappings
    {
        [JsonPropertyName("scope")] public string? Scope { get; set; }
        [JsonPropertyName("notice")] public string? Notice { get; set; }
        [JsonPropertyName("mappings")] public List<ShSymbolMapping>? Mappings { get; set; }
    }

    internal sealed class ShSymbolMappingEnvelope
    {
        [JsonPropertyName("mapping")] public ShSymbolMapping? Mapping { get; set; }
    }

    /// <summary>Display texts for the EODHD budget and a mapping (plain text; backend wording verbatim where it sends one).</summary>
    internal static class ShMapText
    {
        public static string Budget(ShEodhdBudget? b)
        {
            if (b == null) return "EODHD daily calls: not available (the backend did not report the budget)";
            var text = "EODHD calls today: " + (b.UsedToday?.ToString(CultureInfo.InvariantCulture) ?? "?") + " used / "
                       + (b.LeftToday?.ToString(CultureInfo.InvariantCulture) ?? "?") + " left of "
                       + (b.DailyLimit?.ToString(CultureInfo.InvariantCulture) ?? "?")
                       + (string.IsNullOrWhiteSpace(b.Plan) ? "" : " (" + b.Plan + " plan)")
                       + (string.IsNullOrWhiteSpace(b.Resets) ? "" : " · resets " + b.Resets);
            return string.IsNullOrWhiteSpace(b.Message) ? text : text + " — " + b.Message;
        }

        public static string Mapping(ShSymbolMapping m)
            => (m.Isin ?? "?") + " → " + (m.Listing ?? m.Symbol ?? "?")
               + (string.IsNullOrWhiteSpace(m.Currency) ? "" : " · " + m.Currency)
               + (string.IsNullOrWhiteSpace(m.Exchange) ? "" : " · " + m.Exchange)
               + (string.IsNullOrWhiteSpace(m.AssetName) ? "" : " · " + m.AssetName)
               + " · " + (m.PriceSource switch { "eodhd" => "EODHD", "alpaca_us" => "Alpaca (US)", null or "" => "source not reported", _ => m.PriceSource! })
               + (m.ConfirmedVia switch { "dialog" => " · confirmed in dialog", "confirm_flag" => " · confirmed (flag)", _ => "" })
               + (string.IsNullOrWhiteSpace(m.CreatedAt) ? "" : " · " + m.CreatedAt);
    }

    /// <summary>GET /v2/shadow/symbol-check (fix1023 round 2).</summary>
    internal sealed class ShSymbolCheck
    {
        [JsonPropertyName("ticker")] public string? Ticker { get; set; }
        [JsonPropertyName("found")] public bool? Found { get; set; }
        [JsonPropertyName("asset_name")] public string? AssetName { get; set; }
        [JsonPropertyName("exchange")] public string? Exchange { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("tradable")] public bool? Tradable { get; set; }
        [JsonPropertyName("asset_class")] public string? AssetClass { get; set; }
        [JsonPropertyName("usable")] public bool? Usable { get; set; }
        [JsonPropertyName("refused_reason")] public string? RefusedReason { get; set; }
        [JsonPropertyName("mapped_to_isin")] public string? MappedToIsin { get; set; }
        [JsonPropertyName("isin")] public string? Isin { get; set; }
        [JsonPropertyName("isin_valid")] public bool? IsinValid { get; set; }
        [JsonPropertyName("isin_us")] public bool? IsinUs { get; set; }
        [JsonPropertyName("current_mapping")] public ShSymbolCheckMapping? CurrentMapping { get; set; }
    }

    internal sealed class ShSymbolCheckMapping
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("price_source")] public string? PriceSource { get; set; }
    }
}
