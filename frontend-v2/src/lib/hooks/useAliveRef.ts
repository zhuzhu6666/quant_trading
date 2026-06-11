import { useEffect, useRef } from "react";

export function useAliveRef(): React.MutableRefObject<boolean> {
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);
  return aliveRef;
}
