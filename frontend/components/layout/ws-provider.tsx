"use client";
import { useEffect } from "react";
import { getWSClient } from "@/lib/ws";

export function WSProvider({ children }: { children: React.ReactNode }) {
  useEffect(() => {
    const client = getWSClient();
    client.start("/ws/state");
    return () => client.stop();
  }, []);
  return <>{children}</>;
}
