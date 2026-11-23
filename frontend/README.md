# Frontend — development skeleton

React + TypeScript; Vite is only the development/build tool. `npm ci` installs the exact lockfile dependency tree, `npm run build` checks types and creates a production bundle, and `npm run test:smoke` checks the live UI/API proxy while the Windows launcher is running with `-SmokeTest`.

The screen displays the actual local session and API health. Project/media/pipeline actions are explicitly disabled and have no corresponding backend endpoints. The page uses system fonts and makes no third-party requests.
