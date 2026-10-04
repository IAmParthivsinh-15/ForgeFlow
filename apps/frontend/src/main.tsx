import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Link, Route, Routes } from "react-router-dom";

import "./index.css";
import { WorkflowPage } from "./pages/WorkflowPage";
import { WorkflowsPage } from "./pages/WorkflowsPage";

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 1 } } });

function App() {
  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-4 py-3">
          <Link to="/" className="font-semibold tracking-tight">
            ForgeFlow
          </Link>
          <span className="text-xs text-slate-500">Requirement-first engineering workflows</span>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-6">
        <Routes>
          <Route path="/" element={<WorkflowsPage />} />
          <Route path="/workflows/:workflowId" element={<WorkflowPage />} />
        </Routes>
      </main>
    </div>
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
