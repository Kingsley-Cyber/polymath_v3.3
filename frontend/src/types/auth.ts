// Auth-related types for Polymath RAG v3.3

export interface LoginRequest {
  username: string;
  password: string;
}

export interface UserPublic {
  id: string;
  username: string;
  created_at: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: "bearer";
  user: UserPublic;
}

export interface UserMeResponse {
  id: string;
  username: string;
  created_at: string;
}

export interface UpdateCredentialsRequest {
  current_password: string;
  new_username?: string;
  new_password?: string;
}

export interface UpdateCredentialsResponse {
  success: boolean;
  access_token: string;
  user: UserPublic;
}
