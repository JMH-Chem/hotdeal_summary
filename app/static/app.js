// 쿠폰 코드 복사 버튼
document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("[data-copy]");
  if (!btn) return;
  ev.preventDefault();
  try {
    await navigator.clipboard.writeText(btn.dataset.copy);
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = btn.dataset.copy;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  btn.classList.add("copied");
  setTimeout(() => btn.classList.remove("copied"), 1500);
});
