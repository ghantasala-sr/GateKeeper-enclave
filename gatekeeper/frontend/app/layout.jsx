import { IBM_Plex_Mono, IBM_Plex_Sans, Instrument_Serif } from "next/font/google";
import "./globals.css";

const serif = Instrument_Serif({ subsets: ["latin"], weight: "400", style: ["normal", "italic"], variable: "--font-serif" });
const sans = IBM_Plex_Sans({ subsets: ["latin"], weight: ["400", "500", "600"], variable: "--font-sans" });
const mono = IBM_Plex_Mono({ subsets: ["latin"], weight: ["400", "500"], variable: "--font-mono" });

export const metadata = {
  title: "Gatekeeper",
  description: "Company records desk: every answer is cleared by the database before the AI sees it.",
};

// After login the API redirects to /#access_token=... (or #error=...). Take it out of
// the URL BEFORE Next.js starts: the router initialises from the current URL and
// would otherwise write the token back into the address bar and browser history.
// The page picks it up from window.__gkLogin (see takeLoginFragment in page.jsx).
const TAKE_LOGIN_FRAGMENT = `(function () {
  var h = location.hash.slice(1);
  if (/(^|&)(access_token|error)=/.test(h)) {
    window.__gkLogin = h;
    history.replaceState(null, "", location.pathname + location.search);
  }
})();`;

export default function RootLayout({ children }) {
  return (
    <html lang="en" className={`${serif.variable} ${sans.variable} ${mono.variable}`}>
      <head>
        <script dangerouslySetInnerHTML={{ __html: TAKE_LOGIN_FRAGMENT }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
