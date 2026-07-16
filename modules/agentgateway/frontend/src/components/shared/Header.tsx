"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Workflow } from "lucide-react";

export function Header() {
  const pathname = usePathname();

  return (
    <header className="border-b bg-background">
      <div className="flex h-14 items-center gap-4 px-6">
        <Link href="/" className="font-bold text-lg">
          智能体工厂
        </Link>
        <nav className="flex gap-2 ml-6">
          <Link href="/workbench">
            <Button variant={pathname.startsWith("/workbench") || pathname.startsWith("/builder") ? "default" : "ghost"} size="sm">
              <Workflow className="w-4 h-4 mr-1" />
              工作台
            </Button>
          </Link>
          <Link href="/capabilities">
            <Button variant={pathname.startsWith("/capabilities") ? "default" : "ghost"} size="sm">
              能力库
            </Button>
          </Link>
          <Link href="/chat">
            <Button variant={pathname.startsWith("/chat") ? "default" : "ghost"} size="sm">
              对话
            </Button>
          </Link>
        </nav>
      </div>
    </header>
  );
}