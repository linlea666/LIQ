/**
 * 剪贴板写入（安全上下文降级兼容）。
 *
 * navigator.clipboard 仅在安全上下文（HTTPS / localhost）可用；本项目生产
 * 环境通过 HTTP + IP 直连访问，该 API 为 undefined。因此：
 *   1. HTTPS / localhost：navigator.clipboard.writeText
 *   2. HTTP IP / 旧浏览器：fallback 到 document.execCommand('copy') + 临时 textarea
 *
 * 返回是否复制成功。提取自 SweepWatchTracePanel 的等价实现，供各页面复用。
 */
export async function copyTextToClipboard(text: string): Promise<boolean> {
  try {
    if (
      typeof window !== "undefined"
      && window.isSecureContext
      && navigator.clipboard?.writeText
    ) {
      await navigator.clipboard.writeText(text);
      return true;
    }
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-9999px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, text.length);
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}
