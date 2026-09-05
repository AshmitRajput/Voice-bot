import { useParams, useNavigate } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { PageHeader } from "@/components/layout/AppShell";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useCustomerDetail } from "@/hooks/useCustomers";
import { formatCurrency } from "@/lib/utils";

export default function CustomerDetails() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { data, isLoading, isError } = useCustomerDetail(id);

  return (
    <div className="space-y-6">
      <Button variant="ghost" size="sm" className="gap-1 -ml-2" onClick={() => navigate("/customers")}>
        <ArrowLeft className="h-4 w-4" />
        Back to customers
      </Button>

      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}

      {isError && (
        <p className="text-sm text-destructive">
          Couldn't load this customer. Check{" "}
          <code className="rounded bg-muted px-1 py-0.5">/api/admin/customers/{id}/</code>.
        </p>
      )}

      {data && (
        <>
          <PageHeader title={data.customer.name} description={data.customer.phone_number} />

          <div className="grid gap-4 md:grid-cols-3">
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">
                  Total calls
                </CardTitle>
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-semibold tabular-nums">
                  {data.customer.total_calls}
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">
                  Do-not-call
                </CardTitle>
              </CardHeader>
              <CardContent>
                {data.customer.do_not_call ? (
                  <Badge variant="destructive">
                    {data.customer.do_not_call_reason || "Flagged"}
                  </Badge>
                ) : (
                  <span className="text-sm text-muted-foreground">No</span>
                )}
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">
                  Contact
                </CardTitle>
              </CardHeader>
              <CardContent className="text-sm space-y-1">
                <div>{data.customer.email || "No email on file"}</div>
                <div className="text-muted-foreground">
                  Ref: {data.customer.account_reference || "—"}
                </div>
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">Recovery cases</CardTitle>
            </CardHeader>
            <CardContent>
              {data.recovery_cases.length === 0 ? (
                <p className="text-sm text-muted-foreground">No recovery cases yet.</p>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Status</TableHead>
                      <TableHead>Priority</TableHead>
                      <TableHead>Amount due</TableHead>
                      <TableHead>Recovered</TableHead>
                      <TableHead>Due date</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.recovery_cases.map((rc) => (
                      <TableRow
                        key={rc.id}
                        className="cursor-pointer"
                        onClick={() => navigate(`/recovery-cases/${rc.id}`)}
                      >
                        <TableCell>
                          <Badge variant="outline">{rc.status}</Badge>
                        </TableCell>
                        <TableCell>{rc.priority}</TableCell>
                        <TableCell className="font-mono text-sm">
                          {formatCurrency(rc.amount_due)}
                        </TableCell>
                        <TableCell className="font-mono text-sm">
                          {formatCurrency(rc.amount_recovered)}
                        </TableCell>
                        <TableCell className="text-muted-foreground text-sm">
                          {rc.due_date ?? "—"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
