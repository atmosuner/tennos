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
      "Access-Control-Allow-Methods": "POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
      "Vary": "Origin",
    };
    const json = (obj, status) =>
      new Response(JSON.stringify(obj), { status, headers: { ...cors, "Content-Type": "application/json" } });

    if (req.method === "OPTIONS") return new Response(null, { headers: cors });
    if (req.method !== "POST") return json({ error: { message: "method not allowed" } }, 405);
    if (!ALLOWED.has(origin)) return json({ error: { message: "origin not allowed" } }, 403);

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
