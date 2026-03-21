export default function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  const { password } = req.body || {};
  const correct = process.env.SITE_PASSWORD;
  const token = process.env.AUTH_TOKEN;

  if (!password || password !== correct) {
    return res.status(401).json({ error: 'Invalid password' });
  }

  // Set httpOnly cookie — not accessible from JS, sent automatically with requests
  res.setHeader('Set-Cookie', [
    `elk-auth=${token}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=2592000`
  ]);

  return res.status(200).json({ ok: true });
}
