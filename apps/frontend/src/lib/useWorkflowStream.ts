import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import type { WorkflowEvent } from "./api";

/**
 * Subscribes to the workflow's Server-Sent Events stream. Every event appends to the
 * cached event list and refreshes the workflow detail, so the page follows the
 * workflow live without polling.
 */
export function useWorkflowStream(workflowId: string) {
  const queryClient = useQueryClient();

  useEffect(() => {
    const source = new EventSource(`/api/v1/workflows/${workflowId}/stream`);
    const onEvent = (message: MessageEvent<string>) => {
      const event = JSON.parse(message.data) as WorkflowEvent;
      queryClient.setQueryData<WorkflowEvent[]>(["events", workflowId], (previous = []) =>
        previous.some((e) => e.seq === event.seq) ? previous : [...previous, event],
      );
      queryClient.invalidateQueries({ queryKey: ["workflow", workflowId] });
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
    };
    // Named SSE events (event: <type>) do not reach onmessage, so listen generically.
    const types = [
      "workflow.created",
      "workflow.status_changed",
      "workflow.routed",
      "workflow.failed",
      "requirement.analysis_requested",
      "requirement.specification_created",
      "clarification.requested",
      "clarification.answered",
      "agent.run.completed",
      "agent.run.failed",
      "workflow.execution_started",
      "workflow.execution_finished",
      "task.created",
      "task.ready",
      "task.dispatched",
      "task.started",
      "task.completed",
      "task.failed",
      "task.blocked",
      "task.unblocked",
      "task.retrying",
      "task.cancelled",
      "workspace.created",
      "test.completed",
      "integration.conflict",
    ];
    types.forEach((t) => source.addEventListener(t, onEvent as EventListener));
    source.onmessage = onEvent;
    return () => source.close();
  }, [workflowId, queryClient]);
}
