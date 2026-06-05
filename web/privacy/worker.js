const HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mori Plugin — Privacy Policy</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         max-width: 720px; margin: 3rem auto; padding: 0 1.25rem; color: #1a1a1a; background: #fff; }
  @media (prefers-color-scheme: dark) { body { color: #e6e6e6; background: #161616; } a { color: #7aa2f7; } code { background:#222; } }
  h1 { font-size: 1.7rem; margin-bottom: .25rem; }
  h2 { font-size: 1.15rem; margin-top: 2rem; }
  .updated { color: #777; font-size: .9rem; margin-bottom: 2rem; }
  .lead { font-size: 1.05rem; }
  code { background: #f2f2f2; padding: .1em .35em; border-radius: 4px; font-size: .92em; }
  a { color: #2b6cb0; }
  footer { margin-top: 3rem; color: #777; font-size: .9rem; border-top: 1px solid #8884; padding-top: 1rem; }
</style>
</head>
<body>
  <h1>Mori Plugin — Privacy Policy</h1>
  <div class="updated">Last updated: 5 June 2026</div>

  <p class="lead"><strong>Mori is open-source (AGPL-3.0) and self-hosted.</strong> The plugin is a client for a Mori
  server that <strong>you</strong> run. The plugin author collects <strong>no</strong> data, runs no analytics, and
  the plugin performs <strong>no</strong> telemetry or &ldquo;phone-home&rdquo;.</p>

  <h2>What the plugin does</h2>
  <p>When enabled, the Mori plugin captures your coding-session activity &mdash; tool calls, prompts, the assistant&rsquo;s
  reasoning, file paths, working directory, timestamps, and your machine&rsquo;s hostname &mdash; and sends it to the Mori
  server URL <strong>you configure</strong>, so that server can build your shared memory. It sends this to nowhere else.</p>

  <h2>Where your data goes</h2>
  <ul>
    <li><strong>Only to your configured server.</strong> The plugin transmits data exclusively to the <code>server_url</code>
        you set. It never contacts the plugin author, and it sends no plugin data to Anthropic beyond Claude Code&rsquo;s
        own normal operation.</li>
    <li><strong>Your API key stays local.</strong> Your Mori API key is stored in your operating system&rsquo;s secure
        keychain (or local credentials store), never in plaintext settings.</li>
    <li><strong>No phone-home.</strong> The plugin&rsquo;s only network calls are to the server URL you provide &mdash;
        including a startup health check against that same URL. It contacts no other endpoint.</li>
  </ul>

  <h2>Your server, your data</h2>
  <p>Because Mori is self-hosted, <strong>you</strong> control all stored data &mdash; retention, access, and deletion
  &mdash; on <strong>your own</strong> infrastructure. Your Mori server may call an LLM provider that you configure (for
  its memory-distillation pipeline) under your own provider account and that provider&rsquo;s terms; the plugin author is
  not party to that.</p>

  <h2>Data the author collects</h2>
  <p>None. The author operates no service that receives your data and performs no tracking or analytics on plugin usage.</p>

  <h2>Open source &amp; auditability</h2>
  <p>The complete source is available under AGPL-3.0 at
  <a href="https://github.com/fjwood69/mori">github.com/fjwood69/mori</a> &mdash; you can audit exactly what the plugin
  sends and where.</p>

  <h2>Changes</h2>
  <p>If this policy changes, the &ldquo;Last updated&rdquo; date above will change and the new version will be published here.</p>

  <h2>Contact</h2>
  <p>Questions or concerns: open an issue at
  <a href="https://github.com/fjwood69/mori/issues">github.com/fjwood69/mori/issues</a>.</p>

  <footer>Mori &mdash; self-hosted, cross-device memory for AI coding agents. AGPL-3.0.</footer>
</body>
</html>`;

addEventListener("fetch", (event) => {
  event.respondWith(
    new Response(HTML, {
      headers: {
        "content-type": "text/html; charset=UTF-8",
        "cache-control": "public, max-age=3600",
      },
    })
  );
});
