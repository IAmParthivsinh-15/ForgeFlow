import { Background, Controls, Handle, Position, ReactFlow, type Edge, type Node, type NodeProps } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useMemo } from "react";

import type { Task, TaskStatus } from "../lib/api";

/** One colour per task status, used consistently in the graph and the side panel (spec section 64). */
export const TASK_STATUS_STYLE: Record<TaskStatus, string> = {
  PENDING: "border-slate-300 bg-white text-slate-600 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300",
  READY: "border-sky-300 bg-sky-50 text-sky-900 dark:border-sky-800 dark:bg-sky-950 dark:text-sky-200",
  DISPATCHED: "border-sky-400 bg-sky-50 text-sky-900 dark:border-sky-700 dark:bg-sky-950 dark:text-sky-200",
  RUNNING: "border-sky-500 bg-sky-100 text-sky-900 dark:border-sky-500 dark:bg-sky-900 dark:text-sky-100",
  COMPLETED: "border-emerald-400 bg-emerald-50 text-emerald-900 dark:border-emerald-700 dark:bg-emerald-950 dark:text-emerald-200",
  FAILED: "border-rose-400 bg-rose-50 text-rose-900 dark:border-rose-700 dark:bg-rose-950 dark:text-rose-200",
  BLOCKED: "border-amber-400 bg-amber-50 text-amber-900 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-200",
  CANCELLED: "border-slate-300 bg-slate-100 text-slate-500 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-400",
  RETRYING: "border-violet-400 bg-violet-50 text-violet-900 dark:border-violet-700 dark:bg-violet-950 dark:text-violet-200",
};

const NODE_WIDTH = 220;
const COLUMN_GAP = 70;
const ROW_GAP = 26;
const NODE_HEIGHT = 92;

type TaskNodeData = { task: Task; selected: boolean };

function TaskNode({ data }: NodeProps<Node<TaskNodeData>>) {
  const { task, selected } = data;
  const files = task.result?.files_changed.length;
  return (
    <div
      className={`w-[220px] rounded-xl border-2 px-3 py-2 text-left shadow-sm transition ${TASK_STATUS_STYLE[task.status]} ${
        selected ? "ring-2 ring-slate-900 ring-offset-2 dark:ring-white dark:ring-offset-slate-950" : ""
      }`}
    >
      <Handle type="target" position={Position.Left} className="!bg-slate-400" />
      <div className="flex items-center justify-between gap-2 text-[11px] font-medium uppercase tracking-wide opacity-80">
        <span>{task.key}</span>
        <span className="flex items-center gap-1">
          {(task.status === "RUNNING" || task.status === "DISPATCHED") && (
            <span className="size-1.5 animate-pulse rounded-full bg-current" />
          )}
          {task.status.toLowerCase()}
        </span>
      </div>
      <div className="mt-0.5 truncate text-sm font-semibold" title={task.title}>
        {task.title}
      </div>
      <div className="mt-1 truncate text-xs opacity-75">
        {task.kind === "implement" ? (task.file_scope.join(", ") || "no scope") : task.agent_type.replace("_", " ")}
      </div>
      <div className="mt-0.5 text-[11px] opacity-70">
        {task.wait_reason ?? (files !== undefined ? `${files} file(s) changed` : `attempt ${task.attempt}/${task.max_attempts}`)}
      </div>
      <Handle type="source" position={Position.Right} className="!bg-slate-400" />
    </div>
  );
}

const nodeTypes = { task: TaskNode };

/** Columns = dependency depth, so parallel tasks stack vertically in the same column. */
function layout(tasks: Task[]): Map<string, { x: number; y: number }> {
  const byId = new Map(tasks.map((t) => [t.task_id, t]));
  const depth = new Map<string, number>();
  const depthOf = (t: Task, seen = new Set<string>()): number => {
    if (depth.has(t.task_id)) return depth.get(t.task_id)!;
    if (seen.has(t.task_id)) return 0;
    seen.add(t.task_id);
    const deps = t.dependencies.map((d) => byId.get(d)).filter((d): d is Task => !!d);
    // Subtasks without dependencies still come after the planning task that created them.
    const implicit = t.kind !== "decompose" && deps.length === 0 ? 1 : 0;
    const d = Math.max(implicit, ...deps.map((x) => depthOf(x, seen) + 1));
    depth.set(t.task_id, d);
    return d;
  };
  const columns = new Map<number, Task[]>();
  for (const t of tasks) {
    const d = depthOf(t);
    columns.set(d, [...(columns.get(d) ?? []), t]);
  }
  const tallest = Math.max(...[...columns.values()].map((c) => c.length));
  const positions = new Map<string, { x: number; y: number }>();
  for (const [d, column] of columns) {
    const offset = ((tallest - column.length) * (NODE_HEIGHT + ROW_GAP)) / 2;
    column.forEach((t, i) => {
      positions.set(t.task_id, { x: d * (NODE_WIDTH + COLUMN_GAP), y: offset + i * (NODE_HEIGHT + ROW_GAP) });
    });
  }
  return positions;
}

export function TaskGraph({
  tasks,
  selectedId,
  onSelect,
}: {
  tasks: Task[];
  selectedId: string | null;
  onSelect: (taskId: string) => void;
}) {
  const { nodes, edges, height } = useMemo(() => {
    const positions = layout(tasks);
    const decompose = tasks.find((t) => t.kind === "decompose");
    const nodes: Node<TaskNodeData>[] = tasks.map((task) => ({
      id: task.task_id,
      type: "task",
      position: positions.get(task.task_id) ?? { x: 0, y: 0 },
      data: { task, selected: task.task_id === selectedId },
      draggable: false,
    }));
    const edges: Edge[] = [];
    for (const task of tasks) {
      const sources = task.dependencies.length
        ? task.dependencies
        : task.kind !== "decompose" && decompose
          ? [decompose.task_id]
          : [];
      for (const source of sources) {
        edges.push({
          id: `${source}->${task.task_id}`,
          source,
          target: task.task_id,
          animated: task.status === "RUNNING",
          style: { strokeWidth: 1.5 },
        });
      }
    }
    const rows = Math.max(1, ...[...positions.values()].map((p) => p.y)) + NODE_HEIGHT;
    return { nodes, edges, height: Math.min(Math.max(rows + 60, 220), 520) };
  }, [tasks, selectedId]);

  return (
    <div style={{ height }} className="w-full overflow-hidden rounded-xl border border-slate-200 dark:border-slate-800">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodeClick={(_, node) => onSelect(node.id)}
        fitView
        fitViewOptions={{ padding: 0.15 }}
        nodesConnectable={false}
        proOptions={{ hideAttribution: true }}
        minZoom={0.3}
      >
        <Background gap={18} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
