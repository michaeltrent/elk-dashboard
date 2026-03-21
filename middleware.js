export const config = {
  matcher: ['/((?!api/auth|login\\.html|_next|favicon\\.ico).*)']
};

export default function middleware(request) {
  const cookieHeader = request.headers.get('cookie') || '';
  const cookies = Object.fromEntries(
    cookieHeader.split(';').map(c => c.trim().split('=')).filter(c => c.length === 2)
  );
  const token = cookies['elk-auth'];
  const valid = process.env.AUTH_TOKEN;

  if (token && token === valid) {
    return;
  }

  const url = new URL('/login.html', request.url);
  return Response.redirect(url.toString(), 302);
}
