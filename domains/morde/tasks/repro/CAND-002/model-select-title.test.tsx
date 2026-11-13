import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import userEvent from '@testing-library/user-event';
import { readFileSync } from 'node:fs';
import { webcrypto } from 'node:crypto';
import { resolve } from 'node:path';
import { MemoryRouter } from 'react-router';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import { portalApi } from '../app/api';
import type { components } from '../api/schema';
import { messageOutbox } from '../app/message-outbox';
import type { PendingConversationMessage } from '../app/message-outbox';
import type { MainConversation } from '../app/api';
import { ToastProvider } from '../app/toast';
import { Assistant, assistantRecentConversationKey, mergeTaskAssistantMessage, resolveTaskObservationURL } from './Assistant';

const authUser = vi.hoisted(() => ({ id: 'u1234567' }));
vi.mock('../app/auth', () => ({ useAuth: () => ({ user: authUser }) }));
const pdfMock = vi.hoisted(() => ({ getDocument: vi.fn() }));
vi.mock('pdfjs-dist', () => ({ GlobalWorkerOptions: { workerSrc: '' }, getDocument: pdfMock.getDocument }));
const tesseractMock = vi.hoisted(() => ({ createWorker: vi.fn() }));
vi.mock('tesseract.js', () => ({ createWorker: tesseractMock.createWorker, OEM: { LSTM_ONLY: 1 } }));

const timestamp = '2026-10-22T08:00:00.000Z';
type ConversationMessage = Awaited<ReturnType<typeof portalApi.conversationMessages>>['items'][number];
type Task = Awaited<ReturnType<typeof portalApi.task>>;
type AcceptConversationMessageRequest = components['schemas']['AcceptConversationMessageRequest'];
const clientContract = JSON.parse(readFileSync(
  resolve(process.cwd(), '../../server/openapi/fixtures/morde-client-v1.json'), 'utf8',
)) as {
  capabilities: Record<'unsupported' | 'queued_task_api' | 'cloud_execution' | 'unsupported_version',
    Awaited<ReturnType<typeof portalApi.capabilities>>>;
};
const model = {
  id: 'p1234567', name: '团队模型', base_url: 'https://models.example.test/v1', model_name: 'morde-test', shared: true, has_api_key: true,
  capabilities: { text: true, tools: true, vision: false, text_stream: false }, checked_at: timestamp, created_at: timestamp, updated_at: timestamp,
} satisfies Awaited<ReturnType<typeof portalApi.modelConnections>>['items'][number];
const mcpConnector = {
  id: 'k1234567', name: '日历工具', endpoint_url: 'https://mcp.example.test/mcp', enabled: true,
  has_credential: true, credential_version: 1, revision: 2, created_at: timestamp, updated_at: timestamp,
} satisfies Awaited<ReturnType<typeof portalApi.mcpConnectors>>['items'][number];
type AvailableModels = Awaited<ReturnType<typeof portalApi.availableModels>>;
const defaultAvailableModels: AvailableModels = {
  items: [{ id: model.id, name: model.name, model_name: model.model_name, capabilities: model.capabilities }],
  configuration_status: 'configured',
  default_model_configured: true,
  default_model_id: model.id,
};
let availableModelsResponse = defaultAvailableModels;
const server = setupServer(http.get('*/api/available-models', () => HttpResponse.json(availableModelsResponse)));
const otherModel = { ...model, id: 'p7654321', name: '备用模型' };
const targetHost: Awaited<ReturnType<typeof portalApi.hosts>>['items'][number] = {
  id: 'h1234567', name: '测试电脑', owner_id: 'u1234567', template_id: 'std', isolation: 'container', state: 'running', state_reason: '',
  runtime_generation: 1, control_version: 2, control_state: 'ai', url: 'https://h1234567.hosts.test',
  workspaces: [{ workspace: { id: 'w1234567', name: '项目工作区', revision: 5, used_bytes: 20, state: 'ready' },
    directory: 'projects/w1234567', state: 'active', binding_version: 1, desired_revision: 5, applied_revision: 5, sync_state: 'healthy', sync_conflicts: [] }],
  resources: { cpu_milli: 500, mem_mib: 1024, cpu_limit_milli: 1000, mem_limit_mib: 2048 },
  expires_at: null, last_active_at: null, created_at: timestamp,
};
const conversation = { id: 'c1234567', scope: 'personal' as const, is_main: true, title: '你好', created_at: timestamp, updated_at: timestamp };
const message = { id: 'm1234567', conversation_id: conversation.id, sequence: 1, role: 'user' as const, content: '你好', task_id: 'q1234567', delivery_status: 'applied' as const, created_at: timestamp };
const reply = { id: 'm7654321', conversation_id: conversation.id, sequence: 2, role: 'assistant' as const, content: '你好！', task_id: 'q1234567', delivery_status: 'applied' as const, created_at: timestamp };
const task = {
  id: 'q1234567', owner_id: 'u1234567', initiator_id: 'u1234567', execution_location: 'server' as const, conversation_id: conversation.id,
  model_id: model.id, objective: message.content, status: 'completed' as const, event_sequence: 0, current_step: null,
  step_count: 0, completed_step_count: 0,
  budget: { active_seconds: 600, tool_calls: 10, model_tokens: 1000 }, usage: { active_seconds: 2, tool_calls: 0, model_tokens: 12 },
  dependencies: [], child_jobs: [], result_review: null,
  created_at: timestamp, updated_at: timestamp, result: reply.content, waiting_reason: null, steps: [], batch_items: [],
  artifacts: [], pending_request: null, clarification_request: null, stop_request: null, assistant_message: null, finished_at: timestamp,
} satisfies Awaited<ReturnType<typeof portalApi.task>>;

// Older server snapshots predate the required K2 projection fields.
function asLegacyTask(value: Task): Task {
  const legacy: Record<string, unknown> = { ...value };
  delete legacy.dependencies;
  delete legacy.child_jobs;
  delete legacy.result_review;
  return legacy as unknown as Task;
}
let acceptedMessageSequence = 0;

function acceptedReceipt(conversationID: string, body: AcceptConversationMessageRequest): Awaited<ReturnType<typeof portalApi.acceptConversationMessage>> {
  const sequence = ++acceptedMessageSequence;
  const submitted: ConversationMessage = {
    id: `m${String(sequence).padStart(7, '0')}`, conversation_id: conversationID,
    sequence: message.sequence + sequence, role: 'user', content: body.content,
    client_message_id: body.client_message_id, attachment_ids: [], routing_status: 'pending',
    delivery_status: 'pending', created_at: timestamp,
  };
  return { message: submitted, replayed: false };
}

function renderPage(initialState: unknown = null) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const view = render(<QueryClientProvider client={client}><ToastProvider><MemoryRouter initialEntries={[{ pathname: '/assistant', state: initialState }]}><Assistant /></MemoryRouter></ToastProvider></QueryClientProvider>);
  return { ...view, client };
}

function mockCommon(options: { conversations?: typeof conversation[]; messages?: ConversationMessage[]; mainConversation?: MainConversation; cloudTasks?: boolean; capabilities?: Awaited<ReturnType<typeof portalApi.capabilities>>; hosts?: typeof targetHost[] } = {}) {
  const standard = options.cloudTasks === false ? clientContract.capabilities.queued_task_api : clientContract.capabilities.cloud_execution;
  vi.spyOn(portalApi, 'capabilities').mockResolvedValue(options.capabilities ??
    { ...standard, supported_features: [...standard.supported_features, 'conversation_auto_routing_v1'] });
  vi.spyOn(portalApi, 'conversations').mockResolvedValue({ items: options.conversations ?? [], next_cursor: null });
  vi.spyOn(portalApi, 'conversation').mockResolvedValue(conversation);
  vi.spyOn(portalApi, 'mainConversation').mockResolvedValue(options.mainConversation ?? (options.conversations?.[0]
    ? { status: 'ready', conversation_id: options.conversations[0].id, version: 1 }
    : { status: 'selection_required', conversation_id: null, version: 0 }));
  vi.spyOn(portalApi, 'ensureMainConversation').mockResolvedValue({ status: 'ready', conversation_id: conversation.id, version: 1 });
  vi.spyOn(portalApi, 'setMainConversation').mockImplementation(async (conversationID) => ({ status: 'ready', conversation_id: conversationID, version: 2 }));
  vi.spyOn(portalApi, 'conversationMessages').mockResolvedValue({ items: options.messages ?? [], next_cursor: null });
  vi.spyOn(portalApi, 'acceptConversationMessage').mockImplementation(async (conversationID, body) => acceptedReceipt(conversationID, body));
  vi.spyOn(portalApi, 'conversationTasks').mockResolvedValue({ items: [], next_cursor: null });
  vi.spyOn(portalApi, 'task').mockImplementation(async (id) => ({ ...task, id }));
  vi.spyOn(portalApi, 'modelConnections').mockResolvedValue({ items: [model, otherModel], configuration_status: 'configured' });
  vi.spyOn(portalApi, 'mcpConnectors').mockResolvedValue({ items: [] });
  vi.spyOn(portalApi, 'preferences').mockResolvedValue({ assistant_name: 'Morde', selected_model_id: model.id, default_host_id: null, updated_at: timestamp });
  vi.spyOn(portalApi, 'hosts').mockResolvedValue({ items: options.hosts ?? [] });
  vi.spyOn(portalApi, 'execution').mockResolvedValue({ owner_id: 'u1234567', state: 'enabled', epoch: 1, pending_task_count: 0,
    runtime: { state: 'running', runtime_id: 'rt1234567', runtime_generation: 1, max_concurrent_tasks: 4, active_task_count: 0 }, updated_at: timestamp });
}

describe('CAND-002 model select title (repro)', () => {
  beforeAll(() => server.listen());
  beforeEach(() => {
    availableModelsResponse = defaultAvailableModels;
    acceptedMessageSequence = 0;
    tesseractMock.createWorker.mockReset();
    const values = new Map<string, PendingConversationMessage[]>();
    const orderings = new Map<string, { heads: Record<string, string>; aliases: Record<string, string> }>();
    vi.spyOn(messageOutbox, 'read').mockImplementation(async (scope) => values.get(scope) ?? []);
    vi.spyOn(messageOutbox, 'update').mockImplementation(async (scope, change) => {
      const ordering = orderings.get(scope) ?? { heads: {}, aliases: {} };
      const result = change(values.get(scope) ?? [], ordering);
      orderings.set(scope, ordering);
      values.set(scope, result);
      return result;
    });
  });
  afterEach(() => { cleanup(); server.resetHandlers(); localStorage.clear(); authUser.id = 'u1234567'; vi.restoreAllMocks(); vi.unstubAllGlobals(); });
  afterAll(() => server.close());

  it('exposes the selected model full name as the select title', async () => {
    mockCommon({ conversations: [conversation], messages: [message] });
    renderPage();

    const selector = await screen.findByRole('combobox', { name: '选择 AI 模型' });
    expect(selector).toHaveValue(model.id);
    expect(selector.getAttribute('title')).toBe(`${model.name} · ${model.model_name}`);
  });
});
