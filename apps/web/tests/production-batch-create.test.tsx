import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

const { routerPush, stableRouter } = vi.hoisted(() => {
  const push = vi.fn();
  return { routerPush: push, stableRouter: { push, replace: vi.fn(), back: vi.fn() } };
});

vi.mock('next/navigation', () => ({
  useParams: () => ({ unitId: 'unit-1' }),
  useRouter: () => stableRouter,
}));
vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return { ...actual, apiFetch: vi.fn() };
});
vi.mock('@/components/ui-polish', async () => {
  const actual =
    await vi.importActual<typeof import('@/components/ui-polish')>('@/components/ui-polish');
  return { ...actual, toast: vi.fn() };
});

import UnitBatchesPage from '@/app/units/[unitId]/page';
import { ApiError, apiFetch } from '@/lib/api';
import type {
  CurrentUser,
  Farm,
  ProductionBatch,
  ProductionSite,
  ProductionUnit,
  ProductionUnitType,
} from '@/lib/types';

const mockedApiFetch = apiFetch as unknown as ReturnType<typeof vi.fn>;

const UNIT: ProductionUnit = {
  id: 'unit-1',
  site_id: 'site-1',
  unit_type_id: 'type-1',
  name: 'Hatch Tank 1',
  code: 'HT-01',
  status: 'active',
  capacity: 10000,
};
const SITE: ProductionSite = {
  id: 'site-1',
  farm_id: 'farm-1',
  name: 'Main Site',
  code: 'MAIN',
  status: 'active',
};
const FARM: Farm = {
  id: 'farm-1',
  organization_id: 'org-1',
  name: 'Test Farm',
  code: 'FARM',
  deleted_at: null,
};
const TYPE: ProductionUnitType = {
  id: 'type-1',
  organization_id: null,
  code: 'HATCHERY_TANK',
  name: 'hatchery_tank',
  display_name: 'Hatchery Tank',
  plural_name: 'Hatchery Tanks',
  vertical: 'aquaculture',
  category: 'tank',
  is_system: true,
};
const USER: CurrentUser = {
  id: 'user-1',
  email: 'manager@example.com',
  full_name: 'Farm Manager',
  is_active: true,
  is_verified: true,
  is_superuser: false,
  permissions: [],
  permission_scopes: [
    {
      organization_id: 'org-1',
      farm_id: 'farm-1',
      permissions: ['production_batch.create'],
    },
  ],
};
const CREATED: ProductionBatch = {
  id: 'batch-1',
  unit_id: 'unit-1',
  code: 'BATCH-001',
  state: 'planned',
  species: 'L. vannamei',
  planned_at: '2026-08-04T10:30:00.000Z',
  stocked_at: null,
  harvested_at: null,
  closed_at: null,
  expected_quantity: 5000,
  notes: 'First cycle',
};

function baseMock({
  batches = [],
  user = USER,
  unit = UNIT,
  site = SITE,
}: {
  batches?: ProductionBatch[];
  user?: CurrentUser;
  unit?: ProductionUnit;
  site?: ProductionSite;
} = {}) {
  mockedApiFetch.mockImplementation((path: string) => {
    if (path === '/v1/units/unit-1') return Promise.resolve(unit);
    if (path === '/v1/units/unit-1/batches') return Promise.resolve(batches);
    if (path === '/v1/production-unit-types') return Promise.resolve([TYPE]);
    if (path === '/v1/auth/me') return Promise.resolve(user);
    if (path === '/v1/sites/site-1') return Promise.resolve(site);
    if (path === '/v1/farms/farm-1') return Promise.resolve(FARM);
    return Promise.reject(new Error(`Unexpected request: ${path}`));
  });
}

async function openDialog(testId = 'unit-create-batch-empty') {
  await waitFor(() => expect(screen.getByText('No batches yet')).toBeInTheDocument());
  fireEvent.click(screen.getByTestId(testId));
  expect(
    await screen.findByRole('dialog', { name: 'Create Production Batch' }),
  ).toBeInTheDocument();
}

function fillValidForm() {
  fireEvent.change(screen.getByTestId('production-batch-field-code'), {
    target: { value: 'BATCH-001' },
  });
  fireEvent.change(screen.getByTestId('production-batch-field-species'), {
    target: { value: 'L. vannamei' },
  });
  fireEvent.change(screen.getByTestId('production-batch-field-planned-at'), {
    target: { value: '2026-08-04T10:30' },
  });
  fireEvent.change(screen.getByTestId('production-batch-field-expected-quantity'), {
    target: { value: '5000' },
  });
  fireEvent.change(screen.getByTestId('production-batch-field-notes'), {
    target: { value: 'First cycle' },
  });
}

function mockCreateError(error: unknown) {
  mockedApiFetch.mockImplementation((path: string, init?: RequestInit) => {
    if (path === '/v1/units/unit-1/batches' && init?.method === 'POST') {
      return Promise.reject(error);
    }
    if (path === '/v1/units/unit-1') return Promise.resolve(UNIT);
    if (path === '/v1/units/unit-1/batches') return Promise.resolve([]);
    if (path === '/v1/production-unit-types') return Promise.resolve([TYPE]);
    if (path === '/v1/auth/me') return Promise.resolve(USER);
    if (path === '/v1/sites/site-1') return Promise.resolve(SITE);
    if (path === '/v1/farms/farm-1') return Promise.resolve(FARM);
    return Promise.reject(new Error(`Unexpected request: ${path}`));
  });
}

describe('Production Batch creation', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
  });

  it('renders header and empty-state actions for an authorized user', async () => {
    baseMock();
    render(<UnitBatchesPage />);
    await waitFor(() => expect(screen.getByText('No batches yet')).toBeInTheDocument());
    expect(screen.getByTestId('unit-create-batch-header')).toBeInTheDocument();
    expect(screen.getByTestId('unit-create-batch-empty')).toBeInTheDocument();
  });

  it('hides both creation actions without production_batch.create', async () => {
    baseMock({ user: { ...USER, permissions: [], permission_scopes: [] } });
    render(<UnitBatchesPage />);
    await waitFor(() => expect(screen.getByText('No batches yet')).toBeInTheDocument());
    expect(screen.queryByTestId('unit-create-batch-header')).not.toBeInTheDocument();
    expect(screen.queryByTestId('unit-create-batch-empty')).not.toBeInTheDocument();
  });

  it('disables creation when the unit lifecycle does not allow a batch', async () => {
    baseMock({ unit: { ...UNIT, status: 'maintenance' } });
    render(<UnitBatchesPage />);
    await waitFor(() => expect(screen.getByText('No batches yet')).toBeInTheDocument());
    expect(screen.getByTestId('unit-create-batch-header')).toBeDisabled();
    expect(screen.getByTestId('unit-create-batch-empty')).toBeDisabled();
  });

  it('creates a planned batch, closes, renders immediately, refreshes, and links it', async () => {
    let listCalls = 0;
    mockedApiFetch.mockImplementation((path: string, init?: RequestInit) => {
      if (path === '/v1/units/unit-1/batches' && init?.method === 'POST') {
        return Promise.resolve(CREATED);
      }
      if (path === '/v1/units/unit-1/batches') {
        listCalls += 1;
        return Promise.resolve(listCalls === 1 ? [] : [CREATED]);
      }
      if (path === '/v1/units/unit-1') return Promise.resolve(UNIT);
      if (path === '/v1/production-unit-types') return Promise.resolve([TYPE]);
      if (path === '/v1/auth/me') return Promise.resolve(USER);
      if (path === '/v1/sites/site-1') return Promise.resolve(SITE);
      if (path === '/v1/farms/farm-1') return Promise.resolve(FARM);
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });
    render(<UnitBatchesPage />);
    await openDialog();
    fillValidForm();
    fireEvent.click(screen.getByTestId('production-batch-create-submit'));

    const card = await screen.findByTestId('batch-card-BATCH-001');
    expect(card).toHaveTextContent('planned');
    expect(card).toHaveAttribute('href', '/batches/batch-1');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await waitFor(() => expect(listCalls).toBe(2));
    const post = mockedApiFetch.mock.calls.find(
      ([path, init]) => path === '/v1/units/unit-1/batches' && init?.method === 'POST',
    );
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({
      code: 'BATCH-001',
      species: 'L. vannamei',
      planned_at: new Date('2026-08-04T10:30').toISOString(),
      expected_quantity: 5000,
      notes: 'First cycle',
    });
  });

  it('validates required code and non-negative integer quantity locally', async () => {
    baseMock();
    render(<UnitBatchesPage />);
    await openDialog();
    fireEvent.change(screen.getByTestId('production-batch-field-expected-quantity'), {
      target: { value: '-1' },
    });
    fireEvent.click(screen.getByTestId('production-batch-create-submit'));
    expect(await screen.findByText('Code is required.')).toBeInTheDocument();
    expect(screen.getByText(/Expected quantity must be a whole number/i)).toBeInTheDocument();
    expect(mockedApiFetch.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(0);
  });

  it('maps 422 validation errors inline', async () => {
    mockCreateError(
      new ApiError(422, {
        detail: [{ loc: ['body', 'expected_quantity'], msg: 'Input should be at least 0' }],
      } as never),
    );
    render(<UnitBatchesPage />);
    await openDialog();
    fillValidForm();
    fireEvent.click(screen.getByTestId('production-batch-create-submit'));
    expect(await screen.findByText('Input should be at least 0')).toBeInTheDocument();
    expect(screen.getByTestId('production-batch-field-expected-quantity')).toHaveAttribute(
      'aria-describedby',
      'create-batch-expected-quantity-error',
    );
  });

  it('maps a duplicate-code 409 to the code field', async () => {
    mockCreateError(new ApiError(409, { detail: 'duplicate' }));
    render(<UnitBatchesPage />);
    await openDialog();
    fillValidForm();
    fireEvent.click(screen.getByTestId('production-batch-create-submit'));
    expect(await screen.findByText(/batch with this code already exists/i)).toBeInTheDocument();
  });

  it('surfaces lifecycle conflicts without mislabeling them as duplicate codes', async () => {
    mockCreateError(
      new ApiError(409, {
        detail: { code: 'unit_under_maintenance', message: 'blocked' },
      } as never),
    );
    render(<UnitBatchesPage />);
    await openDialog();
    fillValidForm();
    fireEvent.click(screen.getByTestId('production-batch-create-submit'));
    expect(await screen.findByText(/unit is under maintenance/i)).toBeInTheDocument();
    expect(screen.queryByText(/batch with this code already exists/i)).not.toBeInTheDocument();
  });

  it('redirects to login when creation returns 401', async () => {
    mockCreateError(new ApiError(401, { detail: 'expired' }));
    render(<UnitBatchesPage />);
    await openDialog();
    fillValidForm();
    fireEvent.click(screen.getByTestId('production-batch-create-submit'));
    await waitFor(() => expect(routerPush).toHaveBeenCalledWith('/login'));
  });

  it('keeps the dialog open with a permission message on 403', async () => {
    mockCreateError(new ApiError(403, { detail: 'forbidden' }));
    render(<UnitBatchesPage />);
    await openDialog();
    fillValidForm();
    fireEvent.click(screen.getByTestId('production-batch-create-submit'));
    expect(
      await screen.findByText(/don't have permission to create production batches/i),
    ).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('keeps the dialog open with a tenant-safe message on 404', async () => {
    mockCreateError(new ApiError(404, { detail: 'missing' }));
    render(<UnitBatchesPage />);
    await openDialog();
    fillValidForm();
    fireEvent.click(screen.getByTestId('production-batch-create-submit'));
    expect(await screen.findByText('This production unit is not available.')).toBeInTheDocument();
  });

  it('surfaces network failure and re-enables submission', async () => {
    mockCreateError(new Error('Network request failed'));
    render(<UnitBatchesPage />);
    await openDialog();
    fillValidForm();
    fireEvent.click(screen.getByTestId('production-batch-create-submit'));
    expect(await screen.findByText('Network request failed')).toBeInTheDocument();
    expect(screen.getByTestId('production-batch-create-submit')).toBeEnabled();
  });

  it('disables the form while pending and prevents duplicate submission', async () => {
    let resolveCreate!: (batch: ProductionBatch) => void;
    const createPromise = new Promise<ProductionBatch>((resolve) => {
      resolveCreate = resolve;
    });
    mockCreateError(new Error('unused'));
    mockedApiFetch.mockImplementation((path: string, init?: RequestInit) => {
      if (path === '/v1/units/unit-1/batches' && init?.method === 'POST') return createPromise;
      if (path === '/v1/units/unit-1') return Promise.resolve(UNIT);
      if (path === '/v1/units/unit-1/batches') return Promise.resolve([]);
      if (path === '/v1/production-unit-types') return Promise.resolve([TYPE]);
      if (path === '/v1/auth/me') return Promise.resolve(USER);
      if (path === '/v1/sites/site-1') return Promise.resolve(SITE);
      if (path === '/v1/farms/farm-1') return Promise.resolve(FARM);
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });
    render(<UnitBatchesPage />);
    await openDialog();
    fillValidForm();
    const submit = screen.getByTestId('production-batch-create-submit');
    fireEvent.click(submit);
    fireEvent.click(submit);
    await waitFor(() => expect(submit).toBeDisabled());
    expect(screen.getByTestId('production-batch-field-code')).toBeDisabled();
    expect(mockedApiFetch.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
    resolveCreate(CREATED);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('focuses code first and restores focus to the opening trigger', async () => {
    baseMock();
    render(<UnitBatchesPage />);
    await waitFor(() => expect(screen.getByText('No batches yet')).toBeInTheDocument());
    const trigger = screen.getByTestId('unit-create-batch-header');
    trigger.focus();
    fireEvent.click(trigger);
    const code = await screen.findByTestId('production-batch-field-code');
    await waitFor(() => expect(code).toHaveFocus());
    fireEvent.click(screen.getByTestId('production-batch-create-cancel'));
    expect(trigger).toHaveFocus();
  });
});
