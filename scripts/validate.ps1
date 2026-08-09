param(
    [switch]$Container
)

$arguments = @('scripts/validate.py')
if ($Container) {
    $arguments += '--container'
}

python @arguments
exit $LASTEXITCODE
