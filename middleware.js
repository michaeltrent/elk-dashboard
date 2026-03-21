import { NextResponse } from 'next/server';

export const config = {
  matcher: ['/((?!api/auth|login.html|_next|favicon.ico).*)']
};

export default function middleware(request) {
  const token = request.cookies.get('elk-auth')?.value;
  const valid = process.env.AUTH_TOKEN;

  if (token && token === valid) {
    return NextResponse.next();
  }

  const loginUrl = new URL('/login.html', request.url);
  return NextResponse.redirect(loginUrl);
}
