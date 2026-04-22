// Kiosk 全螢幕監控：離開全螢幕時通知後端重啟瀏覽器。
// 三個頁面 (return_book / admin / login) 共用。
(function () {
    let notified = false;
    let checkTimer = null;

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
    // 初次載入給 Firefox --kiosk 充足時間完成視窗配置。
    scheduleCheck('initial-load', 6000);
})();
