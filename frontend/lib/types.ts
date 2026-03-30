export interface User {
  id: number;
  username: string;
  email: string;
  role: "owner" | "admin" | "member";
  org_id: number;
  org_name: string;
  is_superadmin: boolean;
  is_active: boolean;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
}

export interface ApiError {
  detail: string;
}

export interface AccountType {
  id: number;
  name: string;
  account_count: number;
}

export interface Account {
  id: number;
  name: string;
  account_type_id: number;
  account_type_name: string;
  balance: number;
  currency: string;
  is_active: boolean;
}

export interface OrgSetting {
  key: string;
  value: string;
}
