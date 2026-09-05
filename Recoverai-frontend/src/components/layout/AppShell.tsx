import { Link, useLocation } from "react-router-dom";
import {
  LayoutDashboard,
  Users,
  Megaphone,
  PhoneCall,
  AudioLines,
  CalendarClock,
  Bot,
  Mic2,
  BookOpen,
  Settings,
  Search,
  Bell,
  Sparkles,
  Sun,
  Moon,
  ChevronDown,
  Command,
  LogOut,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Badge } from "@/components/ui/badge";
import { CommandPalette } from "./CommandPalette";
import { useAuth } from "@/hooks/useAuth";

// Nav mirrors the 13-model schema only — no Dealer/Branch/Segment/Vehicle/
// Appointment/WhatsApp, since none of those exist in RecoverAI's backend.
const nav = [
  { to: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { to: "/customers", label: "Customers", icon: Users },
  { to: "/campaigns", label: "Campaigns", icon: Megaphone },
  { to: "/recovery-cases", label: "Recovery Cases", icon: Bot },
  { to: "/callbacks", label: "Callbacks", icon: CalendarClock },
  { to: "/recordings", label: "Call Recordings", icon: AudioLines },
  { to: "/voice-test", label: "AI Voice Test", icon: PhoneCall },
  { to: "/personas", label: "Personas", icon: Sparkles },
  { to: "/voices", label: "Voices", icon: Mic2 },
  { to: "/knowledge", label: "Knowledge Base", icon: BookOpen },
] as const;

const secondary = [
  { to: "/settings", label: "Settings", icon: Settings },
] as const;

export function AppShell({ children }: { children: ReactNode }) {
  const { pathname } = useLocation();
  const { user, logout } = useAuth();
  const [dark, setDark] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
  }, [dark]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  const isActive = (to: string) => pathname === to || (to !== "/" && pathname.startsWith(to));

  return (
    <div className="min-h-screen flex w-full bg-background text-foreground">
      {/* Sidebar */}
      <aside className="hidden md:flex w-64 shrink-0 flex-col border-r bg-sidebar text-sidebar-foreground">
        <div className="h-14 flex items-center gap-2 px-4 border-b border-sidebar-accent/40">
          <div className="size-8 rounded-md bg-gradient-to-br from-primary to-[color:var(--ai)] grid place-items-center text-primary-foreground font-display font-bold">
            R
          </div>
          <div className="leading-tight">
            <div className="text-sm font-semibold font-display">RecoverAI</div>
            <div className="text-[10px] uppercase tracking-wider text-sidebar-foreground/60">
              Recovery Agent OS
            </div>
          </div>
        </div>

        <nav className="flex-1 overflow-y-auto p-2 space-y-0.5 pt-3">
          {nav.map((item) => (
            <Link
              key={item.to}
              to={item.to}
              className={cn(
                "flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm transition-colors",
                isActive(item.to)
                  ? "bg-sidebar-accent text-sidebar-accent-foreground font-medium"
                  : "text-sidebar-foreground/80 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground",
              )}
            >
              <item.icon className="size-4" />
              <span className="flex-1">{item.label}</span>
            </Link>
          ))}

          <div className="pt-4 pb-1 px-2.5 text-[10px] uppercase tracking-wider text-sidebar-foreground/50">
            Workspace
          </div>
          {secondary.map((item) => (
            <Link
              key={item.to}
              to={item.to}
              className={cn(
                "flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm transition-colors",
                isActive(item.to)
                  ? "bg-sidebar-accent text-sidebar-accent-foreground font-medium"
                  : "text-sidebar-foreground/80 hover:bg-sidebar-accent/60",
              )}
            >
              <item.icon className="size-4" />
              <span>{item.label}</span>
            </Link>
          ))}
        </nav>

        <div className="p-3 border-t border-sidebar-accent/40">
          <div className="rounded-lg border border-[color:var(--ai)]/25 bg-[color:var(--ai)]/10 p-3">
            <div className="flex items-center gap-2 text-xs font-medium">
              <Sparkles className="size-3.5 text-[color:var(--ai)]" />
              Recovery Agent
            </div>
            <p className="mt-1 text-[11px] text-sidebar-foreground/70 leading-snug">
              Ask about a customer's case, a campaign's performance, or a call.
            </p>
          </div>
        </div>
      </aside>

      {/* Main column */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Topbar */}
        <header className="h-14 flex items-center gap-3 border-b px-4 md:px-6 bg-background/80 backdrop-blur sticky top-0 z-30">
          <button
            onClick={() => setPaletteOpen(true)}
            className="flex-1 max-w-md flex items-center gap-2 rounded-md border bg-muted/40 px-3 py-1.5 text-sm text-muted-foreground hover:bg-muted transition-colors"
          >
            <Search className="size-4" />
            <span className="flex-1 text-left">Search customers, cases, calls…</span>
            <kbd className="hidden sm:inline-flex items-center gap-1 rounded border bg-background px-1.5 py-0.5 text-[10px] font-mono">
              <Command className="size-3" />K
            </kbd>
          </button>

          <div className="flex items-center gap-1 ml-auto">
            <Button variant="ghost" size="icon" onClick={() => setDark((d) => !d)} aria-label="Toggle theme">
              {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
            </Button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="icon" className="relative">
                  <Bell className="size-4" />
                  <span className="absolute top-1.5 right-1.5 size-1.5 rounded-full bg-destructive" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-80">
                <DropdownMenuLabel>Notifications</DropdownMenuLabel>
                <DropdownMenuSeparator />
                <DropdownMenuItem className="flex flex-col items-start gap-0.5">
                  <div className="text-sm font-medium">6 callbacks due today</div>
                  <div className="text-xs text-muted-foreground">Recovery callbacks</div>
                </DropdownMenuItem>
                <DropdownMenuItem className="flex flex-col items-start gap-0.5">
                  <div className="text-sm font-medium">Campaign "Late Payment — Sep" started</div>
                  <div className="text-xs text-muted-foreground">Just now</div>
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>

            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button className="ml-1 flex items-center gap-2 rounded-md px-1.5 py-1 hover:bg-accent">
                  <Avatar className="size-7">
                    <AvatarFallback className="text-xs bg-primary text-primary-foreground">
                      {(user?.name ?? "A").slice(0, 2).toUpperCase()}
                    </AvatarFallback>
                  </Avatar>
                  <div className="hidden lg:block text-left leading-tight">
                    <div className="text-xs font-medium">{user?.name ?? "Admin"}</div>
                    <div className="text-[10px] text-muted-foreground">
                      {user?.is_superuser ? "Superuser" : "Administrator"}
                    </div>
                  </div>
                  <ChevronDown className="size-3.5 text-muted-foreground" />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-48">
                <DropdownMenuItem asChild>
                  <Link to="/settings">Settings</Link>
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem onClick={logout} className="text-destructive focus:text-destructive">
                  <LogOut className="mr-2 size-4" /> Sign out
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </header>

        <main className="flex-1 min-w-0">{children}</main>
      </div>

      <CommandPalette open={paletteOpen} onOpenChange={setPaletteOpen} />
    </div>
  );
}

export function PageHeader({
  title,
  description,
  actions,
  breadcrumbs,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  breadcrumbs?: { label: string; to?: string }[];
}) {
  return (
    <div className="px-4 md:px-6 lg:px-8 pt-6 pb-4 border-b bg-background">
      {breadcrumbs && breadcrumbs.length > 0 && (
        <nav className="text-xs text-muted-foreground mb-2 flex items-center gap-1.5">
          {breadcrumbs.map((b, i) => (
            <span key={i} className="flex items-center gap-1.5">
              {b.to ? <Link to={b.to} className="hover:text-foreground">{b.label}</Link> : <span>{b.label}</span>}
              {i < breadcrumbs.length - 1 && <span>/</span>}
            </span>
          ))}
        </nav>
      )}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-2xl font-display font-semibold tracking-tight">{title}</h1>
          {description && <p className="mt-1 text-sm text-muted-foreground max-w-2xl">{description}</p>}
        </div>
        {actions && <div className="flex items-center gap-2">{actions}</div>}
      </div>
    </div>
  );
}

// Badge kept for pages that want a quick semantic pill without importing
// the full Badge primitive path each time (e.g. campaign/case status chips).
export { Badge as StatusBadge };
