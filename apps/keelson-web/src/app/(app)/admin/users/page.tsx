import type { Metadata } from "next";
import { UsersPage } from "@/components/admin/UsersPage";

export const metadata: Metadata = { title: "Users · Admin" };

export default function AdminUsersRoute() {
  return <UsersPage />;
}
