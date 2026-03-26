export default async function handler(req, res) {
  if (req.method !== "POST") {
    return res.status(405).json({ error: "Method not allowed" });
  }

  const token = process.env.GH_PAT;
  if (!token) {
    return res.status(500).json({ error: "GH_PAT not configured" });
  }

  try {
    const response = await fetch(
      "https://api.github.com/repos/smyang-gif/customer-pipeline-dashboard/actions/workflows/sync.yml/dispatches",
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/vnd.github.v3+json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ ref: "main" }),
      }
    );

    if (response.status === 204) {
      return res.status(200).json({ ok: true, message: "동기화가 시작되었습니다. 1~2분 후 새로고침하세요." });
    } else {
      const text = await response.text();
      return res.status(response.status).json({ ok: false, error: text });
    }
  } catch (e) {
    return res.status(500).json({ ok: false, error: e.message });
  }
}
