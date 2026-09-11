/**
 * Shared TypeScript models mirroring the FastAPI schemas.
 *
 * These types are hand-kept because the monorepo does not yet
 * generate a TypeScript client from OpenAPI. Sprint 3 keeps them
 * intentionally small — we only model what the vertical-slice pages
 * consume.
 */

export type UUID = string;
export type ISODate = string;

// APE core ---------------------------------------------------------

export interface Organization {
  id: UUID;
  name: string;
  slug: string;
}

export interface Farm {
  id: UUID;
  organization_id: UUID;
  name: string;
  code: string;
  deleted_at: string | null;
}

export interface ProductionSite {
  id: UUID;
  farm_id: UUID;
  name: string;
  code: string;
  status: 'active' | 'maintenance' | 'closed';
  description?: string | null;
  deleted_at?: string | null;
}

export interface ProductionUnitType {
  id: UUID;
  organization_id: UUID | null;
  code: string;
  name: string;
  display_name: string;
  plural_name: string | null;
  vertical: string | null;
  category: string | null;
  is_system: boolean;
}

export interface ProductionUnit {
  id: UUID;
  site_id: UUID;
  unit_type_id: UUID;
  name: string;
  code: string;
  status: 'active' | 'maintenance' | 'closed';
  capacity: number | null;
  deleted_at?: string | null;
}

export interface CurrentUser {
  id: UUID;
  email: string;
  full_name: string | null;
  is_active: boolean;
  is_verified: boolean;
  is_superuser: boolean;
  permissions: string[];
  permission_scopes?: Array<{
    organization_id: UUID | null;
    farm_id: UUID | null;
    permissions: string[];
  }>;
}

export type BatchState =
  'planned' | 'stocked' | 'active' | 'harvested' | 'closed' | 'suspended' | 'cancelled' | 'failed';

export interface ProductionBatch {
  id: UUID;
  unit_id: UUID;
  code: string;
  state: BatchState;
  species: string | null;
  planned_at: string | null;
  stocked_at: string | null;
  harvested_at: string | null;
  closed_at: string | null;
  expected_quantity: number | null;
  notes: string | null;
}

export interface ProductionEvent {
  id: UUID;
  batch_id: UUID;
  event_type: string;
  event_type_version: number;
  transfer_id: UUID | null;
  transfer_role: 'out' | 'in' | null;
  performed_by_id: UUID | null;
  performed_at: ISODate;
  data: Record<string, unknown>;
  is_final: boolean;
  notes: string | null;
}

export interface ProductionEventPage {
  items: ProductionEvent[];
  next_cursor: string | null;
  limit: number;
}

export interface EventCatalogEntry {
  code: string;
  display_name: string;
  category: string;
  version: number;
  triggers_transition_to: string | null;
  schema: Record<string, unknown>;
  metadata: Record<string, unknown>;
  openapi_example: Record<string, unknown> | null;
}

export interface BatchProjections {
  batch_id: UUID;
  initial_stocked_quantity: number;
  cumulative_mortality: number;
  cumulative_harvest: number;
  cumulative_transfer_out: number;
  cumulative_transfer_in: number;
  estimated_remaining_population: number;
  latest_average_weight: number | null;
  weight_unit: string | null;
  estimated_biomass_kg: number | null;
  total_feed_kg: number;
  survival_rate: number | null;
  batch_age_days: number | null;
  latest_water_quality: Record<string, unknown> | null;
  latest_sampling_at: string | null;
  computed_at: string;
}
