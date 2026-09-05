import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Search } from "lucide-react";
import { PageHeader } from "@/components/layout/AppShell";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useCustomers } from "@/hooks/useCustomers";

export default function Customers() {
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const navigate = useNavigate();
  const { data, isLoading, isError, isFetching } = useCustomers(page, search);

  const totalPages = data ? Math.max(1, Math.ceil(data.count / 25)) : 1;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Customers"
        description={data ? `${data.count} total` : undefined}
      />

      <div className="relative max-w-sm">
        <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
        <Input
          placeholder="Search name, phone, account ref…"
          className="pl-8"
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            setPage(1);
          }}
        />
      </div>

      {isError && (
        <p className="text-sm text-destructive">
          Couldn't load customers. Check{" "}
          <code className="rounded bg-muted px-1 py-0.5">/api/admin/customers/</code>{" "}
          is reachable.
        </p>
      )}

      {isLoading ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : (
        <>
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Phone</TableHead>
                  <TableHead>Account ref.</TableHead>
                  <TableHead>Total calls</TableHead>
                  <TableHead>DNC</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data?.results.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={5} className="text-center text-muted-foreground py-8">
                      No customers match that search.
                    </TableCell>
                  </TableRow>
                )}
                {data?.results.map((c) => (
                  <TableRow
                    key={c.id}
                    className="cursor-pointer"
                    onClick={() => navigate(`/customers/${c.id}`)}
                  >
                    <TableCell className="font-medium">{c.name}</TableCell>
                    <TableCell className="font-mono text-sm">{c.phone_number}</TableCell>
                    <TableCell className="text-muted-foreground">
                      {c.account_reference || "—"}
                    </TableCell>
                    <TableCell className="tabular-nums">{c.total_calls}</TableCell>
                    <TableCell>
                      {c.do_not_call ? (
                        <Badge variant="destructive">Do not call</Badge>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>

          <div className="flex items-center justify-between">
            <span className="text-sm text-muted-foreground">
              Page {page} of {totalPages}
              {isFetching && " · refreshing…"}
            </span>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={page <= 1}
                onClick={() => setPage((p) => p - 1)}
              >
                Previous
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={page >= totalPages}
                onClick={() => setPage((p) => p + 1)}
              >
                Next
              </Button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
