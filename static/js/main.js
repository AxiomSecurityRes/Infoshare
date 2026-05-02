/* ================================================================
   정보 공유 마당 — main.js
================================================================ */

/* ── 플래시 메시지 자동 닫기 ────────────────────────────────── */
document.addEventListener("DOMContentLoaded", () => {
  setTimeout(() => {
    document.querySelectorAll(".flash").forEach(el => {
      el.style.transition = "opacity .5s, transform .5s";
      el.style.opacity = "0";
      el.style.transform = "translateX(120%)";
      setTimeout(() => el.remove(), 500);
    });
  }, 5000);

  /* ── 알림 카운트 폴링 (30초마다) ───────────────────────── */
  const bell = document.querySelector(".notif-bell .badge");
  if (bell) {
    setInterval(async () => {
      try {
        const r = await fetch("/api/notifications/unread");
        const d = await r.json();
        if (d.count > 0) {
          bell.textContent = d.count;
          bell.style.display = "flex";
        } else {
          bell.style.display = "none";
        }
      } catch (_) {}
    }, 30000);
  }

  /* ── 모바일 네비 오버플로우 제어 ──────────────────────── */
  const nav = document.querySelector(".main-nav");
  if (nav) {
    nav.addEventListener("wheel", (e) => {
      e.preventDefault();
      nav.scrollLeft += e.deltaY;
    }, { passive: false });
  }
});
