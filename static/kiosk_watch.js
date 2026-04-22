// Kiosk 監控：離開全螢幕或焦點離開瀏覽器時，通知後端重啟。
// 三個頁面 (return_book / admin / login) 共用。
(function () {
    const STARTUP_GRACE_MS = 15000;  // 啟動緩衝：Firefox 拿到焦點前的時間
    const BLUR_CONFIRM_MS = 2500;    // blur 後延遲二次確認，容忍條碼掃描器等瞬間焦點切換

    const startTs = Date.now();
    let notified = false;
    let checkTimer = null;

    function inStartupGrace() {
        return (Date.now() - startTs) < STARTUP_GRACE_MS;
    }

    function isFullscreenLike() {
        if (document.fullscreenElement) return true;
        // Firefox --kiosk 不一定設 fullscreenElement，用視窗尺寸做 fallback。
        const tolerance = 80;
        const targetW = Math.max(screen.width || 0, screen.availWidth || 0);
        const targetH = Math.max(screen.height || 0, screen.availHeight || 0);
        return (window.innerWidth || 0) >= (targetW - tolerance)
            && (window.innerHeight || 0) >= (targetH - tolerance);
    }

    async function notify(trigger) {
        if (notified) return;
        notified = true;
        try {
            await fetch('/api/frontend/fullscreen_lost', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ trigger, path: location.pathname, ts: Date.now() })
            });
        } catch (e) {
            console.warn('fullscreen_lost notify failed', e);
        }
    }

    function scheduleCheck(trigger, delayMs) {
        if (notified) return;
        if (checkTimer) clearTimeout(checkTimer);
        checkTimer = setTimeout(() => {
            if (!isFullscreenLike()) notify(trigger);
        }, delayMs);
    }

    // 頁面重啟時清除人工關閉旗標。
    fetch('/api/frontend/session_started', { method: 'POST' }).catch(() => {});

    document.addEventListener('fullscreenchange', () => scheduleCheck('fullscreenchange', 800));
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'hidden') scheduleCheck('visibility-hidden', 300);
    });
    window.addEventListener('resize', () => scheduleCheck('resize', 3000));

    // 焦點離開瀏覽器：威脅模型要求立刻重啟，避免有人操作上層視窗（USB popup、file manager 等）。
    // 為避免 4/21 雪崩重演：(1) 啟動緩衝期內的 blur 一律忽略
    //                    (2) blur 後延遲 BLUR_CONFIRM_MS 再用 document.hasFocus() 二次確認
    // 單純的 window.blur 事件不代表長期丟焦點（條碼掃描器、短暫 USB popup 都可能觸發）。
    window.addEventListener('blur', () => {
        if (notified) return;
        if (inStartupGrace()) return;
        setTimeout(() => {
            if (notified) return;
            if (!document.hasFocus()) notify('focus-lost');
        }, BLUR_CONFIRM_MS);
    });

    // 初次載入給 Firefox --kiosk 充足時間完成視窗配置。
    scheduleCheck('initial-load', 6000);
})();
