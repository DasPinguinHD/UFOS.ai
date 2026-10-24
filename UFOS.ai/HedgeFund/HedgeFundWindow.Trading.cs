using UFOS.ai.HedgeFund.Platform;

namespace UFOS.ai.HedgeFund
{
    /// <summary>
    /// Navigation rail entry "Portfolio &amp; Trading" (Strategy Platform stage 7 — portfolio, risk, OMS, paper only; SPEC §17,
    /// WPF notes §17.13): Platform/TradingView, created on first use like the Loop. The kill switch is NOT part of the view:
    /// the pinned toolbar button (POST /v2/oms/kill since S10 C1) covers the old engine AND every platform account; the view only shows that
    /// scope. The view polls only while it is visible; closing the window stops it for good.
    /// </summary>
    public partial class HedgeFundWindow
    {
        private TradingView? _tradingView;

        private void EnsureTradingView()
        {
            if (_tradingView != null) return;
            _tradingView = new TradingView();
            // stage 8 (SPEC §18.12): "Brokers…" opens Settings (Keys & data) at its Brokers section
            _tradingView.BrokersRequested += () => OpenKeysWindow(brokers: true);
            TradingHost.Child = _tradingView;
        }
    }
}
