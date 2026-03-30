export interface User {
  id: number;
  username: string;
  email: string;
  role: "owner" | "admin" | "member";
  org_id: number;
  org_name: string;
  is_active: boolean;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
}

export interface ApiError {
  detail: string;
}
