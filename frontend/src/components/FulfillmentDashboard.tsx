"use client";

import { useCallback, useEffect, useState } from "react";
import {
  checkBackendHealth,
  createTask,
  fetchTasks,
  updateTaskStatus,
  type HealthCheckResult,
  type Task,
  type TaskStatus,
} from "@/lib/api";
import OrdersSection from "@/components/OrdersSection";
import FulfillmentWorkflowComponent from "@/components/FulfillmentWorkflow";
import AmazonSandboxStatus from "@/components/AmazonSandboxStatus";

// ---------------------------------------------------------------------------
// Status config
// ---------------------------------------------------------------------------

const STATUS_META: Record<
  TaskStatus,
  { label: string; dot: string; bg: string; text: string }
> = {
  pending: {
    label: "Pending",
    dot: "bg-yellow-400",
    bg: "bg-yellow-50",
    text: "text-yellow-700",
  },
  running: {
    label: "Running",
    dot: "bg-blue-400",
    bg: "bg-blue-50",
    text: "text-blue-700",
  },
  completed: {
    label: "Completed",
    dot: "bg-green-400",
    bg: "bg-green-50",
    text: "text-green-700",
  },
  failed: {
    label: "Failed",
    dot: "bg-red-400",
    bg: "bg-red-50",
    text: "text-red-700",
  },
};

const ALL_STATUSES: TaskStatus[] = [
  "pending",
  "running",
  "completed",
  "failed",
];

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function FulfillmentDashboard() {
  // health
  const [health, setHealth] = useState<HealthCheckResult | null>(null);

  // tasks
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // create form
  const [newTitle, setNewTitle] = useState("");
  const [creating, setCreating] = useState(false);

  // ------------------------------------------------------------------
  // Load data
  // ------------------------------------------------------------------

  const loadTasks = useCallback(async () => {
    setLoading(true);
    setError(null);
    const result = await fetchTasks(1, 100);
    if (result.ok) {
      setTasks(result.data.items);
    } else {
      setError(result.error);
    }
    setLoading(false);
  }, []);

  const loadHealth = useCallback(async () => {
    const result = await checkBackendHealth();
    setHealth(result);
  }, []);

  useEffect(() => {
    loadHealth();
    loadTasks();
  }, [loadHealth, loadTasks]);

  // ------------------------------------------------------------------
  // Summary counts
  // ------------------------------------------------------------------

  const summary = ALL_STATUSES.map((s) => ({
    status: s,
    count: tasks.filter((t) => t.status === s).length,
  }));

  // ------------------------------------------------------------------
  // Handlers
  // ------------------------------------------------------------------

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    const title = newTitle.trim();
    if (!title) return;
    setCreating(true);
    const result = await createTask(title);
    if (result.ok) {
      setTasks((prev) => [...prev, result.data]);
      setNewTitle("");
    } else {
      setError(result.error);
    }
    setCreating(false);
  }

  async function handleStatusChange(taskId: string, newStatus: TaskStatus) {
    const result = await updateTaskStatus(taskId, newStatus);
    if (result.ok) {
      setTasks((prev) =>
        prev.map((t) => (t.id === taskId ? result.data : t)),
      );
    } else {
      setError(result.error);
    }
  }

  // ------------------------------------------------------------------
  // Render
  // ------------------------------------------------------------------

  const connected =
    health !== null && health.ok;
  const disconnected =
    health !== null && !health.ok;

  return (
    <div className="min-h-screen bg-gray-50">
      {/* ---- Header ---- */}
      <header className="bg-white border-b border-gray-200 px-6 py-4">
        <div className="max-w-5xl mx-auto flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-gray-900">
              Fulfillment Dashboard
            </h1>
            <p className="text-sm text-gray-500 mt-0.5">
              Amazon AI Fulfillment Assistant
            </p>
          </div>
          <div className="flex items-center gap-2 text-sm">
            {health === null && (
              <span className="text-gray-400">Checking backend…</span>
            )}
            {connected && (
              <span className="inline-flex items-center gap-1.5 text-green-700 font-medium">
                <span className="w-2 h-2 rounded-full bg-green-500" />
                Backend Connected
              </span>
            )}
            {disconnected && (
              <span className="inline-flex items-center gap-1.5 text-red-600 font-medium">
                <span className="w-2 h-2 rounded-full bg-red-500" />
                Backend Disconnected
              </span>
            )}
          </div>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-6 py-8 space-y-8">
        {/* ---- Error banner ---- */}
        {error && (
          <div
            className="bg-red-50 border border-red-200 text-red-700 rounded-lg px-4 py-3 text-sm flex items-start justify-between"
            role="alert"
          >
            <span>{error}</span>
            <button
              onClick={() => setError(null)}
              className="ml-4 text-red-500 hover:text-red-700 font-bold"
              aria-label="Dismiss"
            >
              ×
            </button>
          </div>
        )}

        {/* ---- Summary cards ---- */}
        <section aria-label="Task Summary">
          <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wide mb-3">
            Task Summary
          </h2>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
            {summary.map(({ status, count }) => {
              const meta = STATUS_META[status];
              return (
                <div
                  key={status}
                  className={`rounded-lg border border-gray-200 bg-white p-4 text-center`}
                >
                  <div className="flex items-center justify-center gap-2 mb-1">
                    <span className={`w-2.5 h-2.5 rounded-full ${meta.dot}`} />
                    <span className="text-sm font-medium text-gray-700">
                      {meta.label}
                    </span>
                  </div>
                  <p className="text-3xl font-bold text-gray-900">{count}</p>
                </div>
              );
            })}
          </div>
        </section>

        {/* ---- Create task ---- */}
        <section aria-label="Create Task" className="bg-white border border-gray-200 rounded-lg p-5">
          <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wide mb-3">
            Create Task
          </h2>
          <form onSubmit={handleCreate} className="flex gap-3">
            <input
              type="text"
              value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)}
              placeholder="Enter task title…"
              className="flex-1 rounded-md border border-gray-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            />
            <button
              type="submit"
              disabled={creating || !newTitle.trim()}
              className="px-5 py-2 rounded-md text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 disabled:bg-gray-300 disabled:cursor-not-allowed transition-colors"
            >
              {creating ? "Creating…" : "Add Task"}
            </button>
          </form>
        </section>

        {/* ---- Task list ---- */}
        <section aria-label="Task List">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wide">
              Tasks
            </h2>
            <button
              onClick={loadTasks}
              className="text-sm text-blue-600 hover:text-blue-800 font-medium"
            >
              Refresh
            </button>
          </div>

          {loading && (
            <div className="bg-white border border-gray-200 rounded-lg p-8 text-center text-gray-400">
              Loading tasks…
            </div>
          )}

          {!loading && tasks.length === 0 && (
            <div className="bg-white border border-gray-200 rounded-lg p-8 text-center text-gray-400">
              No tasks yet. Create one above to get started.
            </div>
          )}

          {!loading && tasks.length > 0 && (
            <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
              <table className="w-full text-sm">
                <thead>
                  <tr className="bg-gray-50 border-b border-gray-200 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">
                    <th className="px-4 py-3">Title</th>
                    <th className="px-4 py-3">Status</th>
                    <th className="px-4 py-3">Created</th>
                    <th className="px-4 py-3 text-right">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {tasks.map((task) => {
                    const meta = STATUS_META[task.status];
                    return (
                      <tr key={task.id} className="hover:bg-gray-50">
                        <td className="px-4 py-3">
                          <div className="font-medium text-gray-900">
                            {task.title || (
                              <span className="text-gray-400 italic">
                                Untitled
                              </span>
                            )}
                          </div>
                          <div className="text-xs text-gray-400 font-mono mt-0.5">
                            {task.id.slice(0, 8)}
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          <span
                            className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${meta.bg} ${meta.text}`}
                          >
                            <span
                              className={`w-1.5 h-1.5 rounded-full ${meta.dot}`}
                            />
                            {meta.label}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-gray-500 text-xs">
                          {formatTime(task.created_at)}
                        </td>
                        <td className="px-4 py-3 text-right">
                          <select
                            value={task.status}
                            onChange={(e) =>
                              handleStatusChange(
                                task.id,
                                e.target.value as TaskStatus,
                              )
                            }
                            className="text-xs border border-gray-300 rounded px-2 py-1 bg-white focus:outline-none focus:ring-1 focus:ring-blue-500"
                          >
                            {ALL_STATUSES.map((s) => (
                              <option key={s} value={s}>
                                {STATUS_META[s].label}
                              </option>
                            ))}
                          </select>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>

        {/* ---- Divider ---- */}
        <hr className="border-gray-200" />

        {/* ---- Orders Section ---- */}
        <OrdersSection />

        {/* ---- Divider ---- */}
        <hr className="border-gray-200" />

        {/* ---- Fulfillment Workflow Section ---- */}
        <FulfillmentWorkflowComponent />

        {/* ---- Divider ---- */}
        <hr className="border-gray-200" />

        {/* ---- Amazon Sandbox Status Section ---- */}
        <AmazonSandboxStatus />
      </main>
    </div>
  );
}
