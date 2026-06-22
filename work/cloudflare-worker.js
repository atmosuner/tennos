// Cloudflare Worker — Groq proxy for the Kortex AI evaluation feature.
//
// Holds the Groq key server-side (as a Secret env var GROQ_API_KEY) so it never
// ships to the browser. The static site POSTs {prompt} here; the Worker forwards
// a fixed model/params request to Groq and returns the response.
//
// Deploy: paste into the Cloudflare Worker editor, then add the secret:
//   Settings -> Variables and Secrets -> Add -> Secret -> GROQ_API_KEY = <your-key>
//
// Origin allowlist blocks other websites from spending your Groq quota.

const ALLOWED = new Set([
  "https://atmosuner.github.io",
  "http://localhost:8001",
  "http://127.0.0.1:8001",
]);
const MODEL = "llama-3.3-70b-versatile";

export default {
  async fetch(req, env) {
    const origin = req.headers.get("Origin") || "";
    const cors = {
      "Access-Control-Allow-Origin": ALLOWED.has(origin) ? origin : "null",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
      "Vary": "Origin",
    };
    const json = (obj, status) =>
      new Response(JSON.stringify(obj), { status, headers: { ...cors, "Content-Type": "application/json" } });

    if (req.method === "OPTIONS") return new Response(null, { headers: cors });
    if (!ALLOWED.has(origin)) return json({ error: { message: "origin not allowed" } }, 403);

    // GET /ikort/{id} and /ikort/{id}/klasman — proxy ikort profile pages
    if (req.method === "GET") {
      const pathname = new URL(req.url).pathname;
      const mKlasman = pathname.match(/^\/ikort\/(\d+)\/klasman$/);
      const mProfile = pathname.match(/^\/ikort\/(\d+)$/);
      if (!mKlasman && !mProfile) return json({ error: { message: "not found" } }, 404);

      const pid = (mKlasman || mProfile)[1];
      const ikortBase = `https://ikort.com.tr/oyuncu-profil/${pid}`;
      const hdrs = { "User-Agent": "Mozilla/5.0", "Accept-Language": "tr" };

      const extract = (html, label) => {
        const rx = new RegExp(label + "(?:[^<\\d]|<[^>]*>)*?(\\d+)", "i");
        const hit = html.match(rx);
        return hit ? parseInt(hit[1], 10) : null;
      };

      if (mKlasman) {
        // GET /ikort/{id}/klasman — fetch genelklasman tab, parse table rows
        try {
          const resp = await fetch(`${ikortBase}?page=genelklasman`, { headers: hdrs });
          if (!resp.ok) return json({ error: { message: "ikort " + resp.status } }, 502);
          const html = await resp.text();

          const summary = {
            puan: extract(html, "Genel Klasman Puan"),
            sira: extract(html, "Genel Klasman S[ıi]ra"),
            ulusal: extract(html, "Ulusal Puan"),
            uluslararasi: extract(html, "Uluslararas[\\u0131i] Puan"),
          };

          const stripTags = s => s.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
          // Collect section headings (h2-h5) and data rows by position, then sort
          const items = [];
          const hRe = /<h[2-5]\b[^>]*>([\s\S]*?)<\/h[2-5]>/gi;
          let hm;
          while ((hm = hRe.exec(html)) !== null) {
            const t = stripTags(hm[1]);
            if (t && t.length < 120) items.push({ pos: hm.index, type: "header", cells: [t] });
          }
          const trRe = /<tr\b([^>]*?)>([\s\S]*?)<\/tr>/gi;
          let trm;
          while ((trm = trRe.exec(html)) !== null) {
            const trAttrs = trm[1], inner = trm[2];
            const ths = [], rawTds = [], tds = [];
            const thRe2 = /<th\b[^>]*>([\s\S]*?)<\/th>/gi;
            const tdRe2 = /<td\b[^>]*>([\s\S]*?)<\/td>/gi;
            let m2;
            while ((m2 = thRe2.exec(inner)) !== null) { const t = stripTags(m2[1]); if (t) ths.push(t); }
            while ((m2 = tdRe2.exec(inner)) !== null) { rawTds.push(m2[1]); tds.push(stripTags(m2[1])); }
            if (ths.length > 0 && tds.length === 0) {
              // skip column-header rows
            } else if (tds.length > 0 && tds.some(c => /\d{2}[-./]\d{2}[-./]\d{4}/.test(c))) {
              const trCls = (trAttrs.match(/class=["']([^"']+)/) || [])[1] || '';
              const sayilanRaw = rawTds[0] || '';
              const counted = /pointsticked/i.test(sayilanRaw);
              items.push({ pos: trm.index, type: "data", cells: tds, sayilan: sayilanRaw.slice(0, 800), counted });
            }
          }
          items.sort((a, b) => a.pos - b.pos);
          const rows = items.map(({ type, cells, sayilan, counted }) =>
            type === "header" ? { type, cells } : { type, cells, sayilan, counted });
          return json({ summary, rows }, 200);
        } catch (e) {
          return json({ error: { message: e.message } }, 502);
        }
      }

      // GET /ikort/{id} — genelbilgi: klasman puan + sıra
      try {
        const resp = await fetch(`${ikortBase}?page=genelbilgi`, { headers: hdrs });
        if (!resp.ok) return json({ error: { message: "ikort " + resp.status } }, 502);
        const html = await resp.text();
        return json({
          playerId: +pid,
          ikortUrl: ikortBase,
          klasmanPuan: extract(html, "Genel Klasman Puan"),
          klasmanSira: extract(html, "Genel Klasman S[\\u0131i]ra"),
        }, 200);
      } catch (e) {
        return json({ error: { message: e.message } }, 502);
      }
    }

    if (req.method !== "POST") return json({ error: { message: "method not allowed" } }, 405);

    let body;
    try { body = await req.json(); } catch { return json({ error: { message: "bad json" } }, 400); }
    const prompt = (body && typeof body.prompt === "string") ? body.prompt.slice(0, 8000) : "";
    if (!prompt) return json({ error: { message: "prompt required" } }, 400);

    const groq = await fetch("https://api.groq.com/openai/v1/chat/completions", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + env.GROQ_API_KEY },
      body: JSON.stringify({
        model: MODEL,
        messages: [{ role: "user", content: prompt }],
        temperature: 0.4,
        max_tokens: 1200,
      }),
    });
    const text = await groq.text();
    return new Response(text, { status: groq.status, headers: { ...cors, "Content-Type": "application/json" } });
  },
};
