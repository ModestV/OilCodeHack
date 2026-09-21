import React from "react";
import { createRoot } from "react-dom/client";
import "./fonts.css";
import "./styles.css";
import "./operator-theme.css";
import { App } from "./App";
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
