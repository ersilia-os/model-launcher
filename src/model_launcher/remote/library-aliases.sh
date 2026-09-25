#!/bin/bash
# Library name aliases — source this file, then call resolve_library().
#
# Usage:
#   source /shared/scripts/library-aliases.sh
#   LIBRARY_NAME=$(resolve_library "$1")
#
# Aliases (case-insensitive):
#   Enamine / hit          → Enamine_Hit_Locator_460K
#   Coconut                → Coconut_715K
#   Enamine_Liquid / liquid → Enamine_Liquid_Stock_2.5M
#   Molport                → Molport_Screening_Compounds_5.3M
#   Enamine_Real / real    → Enamine_Real_Sample_10.4M

resolve_library() {
    local input="${1,,}"   # lowercase
    case "$input" in
        enamine_hit_locator_460k|enamine|hit)
            echo "Enamine_Hit_Locator_460K" ;;
        coconut_715k|coconut)
            echo "Coconut_715K" ;;
        enamine_liquid_stock_2.5m|enamine_liquid|liquid)
            echo "Enamine_Liquid_Stock_2.5M" ;;
        molport_screening_compounds_5.3m|molport)
            echo "Molport_Screening_Compounds_5.3M" ;;
        enamine_real_sample_10.4m|enamine_real|real)
            echo "Enamine_Real_Sample_10.4M" ;;
        *)
            echo "$1"  # pass through unchanged if no match
            ;;
    esac
}
