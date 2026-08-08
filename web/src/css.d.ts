// TypeScript 7 (TS2882) refuses a side-effect import it has no declaration for,
// which `import "./styles.css"` in main.ts is. Vite handles the CSS at build
// time and it has no runtime shape worth typing, so an opaque module is enough.
//
// Declared here rather than via `/// <reference types="vite/client" />` because
// tsconfig sets `"types": []` to keep the global scope empty, and vite/client
// would pull its whole ambient surface back in.
declare module "*.css";
