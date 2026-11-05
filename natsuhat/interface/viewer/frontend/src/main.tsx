import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import HostedGate from "./components/HostedGate";
import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <HostedGate><App /></HostedGate>
  </StrictMode>
);
