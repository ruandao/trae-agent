export function accessTokenExpected() {
  return String(process.env.ACCESS_TOKEN || '').trim();
}

export function authMiddleware(req, res, next) {
  const expected = accessTokenExpected();
  if (!expected) {
    res.status(503).json({ detail: 'ACCESS_TOKEN not configured' });
    return;
  }
  const q = req.query?.access_token;
  const h = req.headers['x-access-token'];
  const tok = (typeof q === 'string' ? q : '') || (typeof h === 'string' ? h : '');
  if (tok !== expected) {
    res.status(401).json({ detail: 'Invalid or missing access token' });
    return;
  }
  next();
}
