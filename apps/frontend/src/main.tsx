import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";

import "./index.css";
import { Shell } from "./components/Shell";
import { AutonomyPage } from "./pages/AutonomyPage";
import { ExtensibilityPage } from "./pages/ExtensibilityPage";
import { KnowledgePage } from "./pages/KnowledgePage";
import { RunPage } from "./pages/RunPage";
import { WorkflowPage } from "./pages/WorkflowPage";
import { WorkflowsPage } from "./pages/WorkflowsPage";

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 1 } } });

function App() {
  return (
    <Shell>
      <Routes>
        <Route path="/" element={<WorkflowsPage />} />
        <Route path="/workflows/:workflowId" element={<WorkflowPage />} />
        <Route path="/autonomy" element={<AutonomyPage />} />
        <Route path="/autonomy/runs/:traceId" element={<RunPage />} />
        <Route path="/knowledge" element={<KnowledgePage />} />
        <Route path="/extensibility" element={<ExtensibilityPage />} />
      </Routes>
    </Shell>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
